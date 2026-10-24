"""Deadline-bound supplementary work with persistent progress and partial artifacts.

Each attempt has one deadline, including preparation. A detached watcher enforces
it even between tool calls. This controls process lifetime, not model inference.
"""
from pathlib import Path
from datetime import datetime, timezone
import json
import math
import os
import signal
import subprocess
import sys
import time

from .model import CaseError, read_json, write_json


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def _write(path, value):
    temp = Path(str(path) + '.' + str(os.getpid()) + '.tmp')
    write_json(temp, value)
    # Windows can briefly hold the destination open while the watcher and
    # caller read the same status. Retry that observed sharing conflict only.
    for attempt in range(10):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(.01)


def _event(folder, step, note):
    with (folder / 'progress.jsonl').open('a', encoding='utf-8') as handle:
        handle.write(json.dumps({'time': timestamp(), 'step': step, 'note': note}, ensure_ascii=False) + '\n')


def _files(folder):
    return [str(p.relative_to(folder)).replace('\\', '/') for p in sorted(folder.rglob('*'))
            if p.is_file() and not p.name.endswith('.tmp')]


def _stop(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)

        class Entry(ctypes.Structure):
            _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                        ('heap', ctypes.c_size_t), ('module', w.DWORD), ('threads', w.DWORD),
                        ('parent', w.DWORD), ('priority', w.LONG), ('flags', w.DWORD),
                        ('exe', w.WCHAR * 260)]

        kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
        kernel.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
        kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot == w.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        parents = {}
        entry = Entry(); entry.size = ctypes.sizeof(entry)
        try:
            more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                parents[entry.pid] = entry.parent
                more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(snapshot)
        targets = [pid]
        for parent in targets:
            targets.extend(child for child, ancestor in parents.items() if ancestor == parent and child not in targets)
        for target in reversed(targets):
            handle = kernel.OpenProcess(1 | 0x1000, False, target)
            if not handle:
                if ctypes.get_last_error() == 87:  # already exited
                    continue
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not kernel.TerminateProcess(handle, 1):
                    error = ctypes.get_last_error()
                    exit_code = w.DWORD()
                    if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value == 259:
                        raise ctypes.WinError(error)
            finally:
                kernel.CloseHandle(handle)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _pause(folder):
    registration = read_json(folder / 'registration.json')
    if (folder / 'finished.json').exists():
        return
    execution = read_json(folder / 'execution.json') if (folder / 'execution.json').exists() else None
    termination_error = None
    if execution and execution['status'] == 'running':
        try:
            _stop(execution['pid'])
        except OSError as exc:
            termination_error = str(exc)
    rows = (folder / 'progress.jsonl').read_text(encoding='utf-8').splitlines()
    last = json.loads(rows[-1]) if rows else None
    _write(folder / 'paused.json', {'status': 'pause_failed' if termination_error else 'paused_timeout', 'stopped_at': timestamp(),
                                   'deadline': registration['deadline'], 'last_progress': last,
                                   'execution': execution, 'files': _files(folder),
                                   'termination_error': termination_error,
                                   'reason': '补充方法累计时间已到；保留部分结果，不自动续跑。'})


def status(folder):
    folder = Path(folder).resolve()
    registration = read_json(folder / 'registration.json')
    if (folder / 'paused.json').exists():
        return {**registration, **read_json(folder / 'paused.json')}
    if (folder / 'finished.json').exists():
        return {**registration, **read_json(folder / 'finished.json')}
    if time.time() >= registration['deadline_epoch']:
        _pause(folder)
        return {**registration, **read_json(folder / 'paused.json')}
    return {**registration, 'status': 'active', 'remaining_seconds': registration['deadline_epoch'] - time.time(),
            'files': _files(folder)}


def watch(folder):
    folder = Path(folder)
    deadline = read_json(folder / 'registration.json')['deadline_epoch']
    while time.time() < deadline:
        if (folder / 'finished.json').exists() or (folder / 'paused.json').exists():
            return
        time.sleep(min(.1, max(.001, deadline - time.time())))
    if not (folder / 'finished.json').exists():
        _pause(folder)


def start(run, request, output, seconds=120):
    from .workflow import load_run, _open_round
    run, output = Path(run).resolve(), Path(output).resolve()
    state = load_run(run)
    _open_round(state)
    req = read_json(request)
    for key in ('goal', 'existing_tools_insufficient', 'method'):
        if not isinstance(req.get(key), str) or not req[key].strip():
            raise CaseError('Supplementary method needs ' + key)
    if req.get('variable') not in state['variables']:
        raise CaseError('Supplement only a selected variable')
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
        raise CaseError('Method time budget must be positive and finite')
    if state.get('methods'):
        raise CaseError('One supplementary-method session per case; its budget is not reset')
    case = Path(state['case']).resolve()
    if output == case or case in output.parents or output == run or run in output.parents:
        raise CaseError('Method work must be outside native inputs and RUN')
    output.mkdir(parents=True, exist_ok=False)
    (output / 'outputs').mkdir()
    registration = {**req, 'id': 'supplement', 'started_at': timestamp(),
                    'deadline_epoch': time.time() + seconds, 'budget_seconds': seconds,
                    'inputs': []}
    registration['deadline'] = datetime.fromtimestamp(registration['deadline_epoch'], timezone.utc).isoformat()
    # Freeze chosen inputs; scripts work on these copies and write under outputs/.
    input_dir = output / 'inputs'
    input_dir.mkdir()
    import shutil
    from .core import sha
    for index, name in enumerate(req.get('inputs', []), 1):
        src = Path(name).resolve()
        target = input_dir / (str(index) + '-' + src.name)
        shutil.copy2(src, target)
        registration['inputs'].append({'original': str(src), 'file': str(target.relative_to(output)), 'sha256': sha(target)})
    _write(output / 'registration.json', registration)
    _event(output, 'preparing', '已登记必要性与方法；准备及执行共享同一截止时间。')
    state['methods'] = [str(output)]
    write_json(run / 'workflow.json', state)
    env = dict(os.environ)
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get('PYTHONPATH', '')
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    watcher = subprocess.Popen([sys.executable, '-m', 'powerfault.attempts', str(output)],
                               env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, **options)
    import threading
    threading.Thread(target=watcher.wait, daemon=True).start()
    registration['watcher_pid'] = watcher.pid
    _write(output / 'registration.json', registration)
    return status(output)


def progress(folder, step, note):
    folder = Path(folder).resolve()
    if status(folder)['status'] != 'active':
        raise CaseError('Supplementary method is stopped; no automatic continuation')
    _event(folder, step, note)
    return status(folder)


def execute(folder, script):
    folder, script = Path(folder).resolve(), Path(script).resolve()
    current = status(folder)
    if current['status'] != 'active':
        raise CaseError('Supplementary method is stopped; no automatic continuation')
    if not script.is_relative_to(folder) or not script.is_file():
        raise CaseError('Save the supplementary script inside its registered work directory')
    prior = read_json(folder / 'execution.json') if (folder / 'execution.json').exists() else {}
    if prior.get('status') == 'running' or prior.get('number', 0) >= 2:
        raise CaseError('One execution and one repair at most, within the original deadline')
    from .core import sha
    number = prior.get('number', 0) + 1
    script_copy = folder / ('executed-' + str(number) + '.py')
    script_copy.write_bytes(script.read_bytes())
    _event(folder, 'executing', '执行 ' + script.name)
    options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    with (folder / f'stdout-{number}.log').open('wb') as stdout, (folder / f'stderr-{number}.log').open('wb') as stderr:
        process = subprocess.Popen([sys.executable, '-X', 'utf8', '-u', str(script_copy)], cwd=folder,
                                   stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL, **options)
        execution = {'number': number, 'pid': process.pid, 'status': 'running',
                     'script': script_copy.name, 'script_sha256': sha(script_copy), 'started_at': timestamp()}
        _write(folder / 'execution.json', execution)
        try:
            code = process.wait(timeout=max(.001, current['deadline_epoch'] - time.time()))
        except subprocess.TimeoutExpired:
            _pause(folder)
            if process.poll() is None:
                process.kill()
            process.wait()
            code = process.returncode
    execution.update(status='completed' if code == 0 else 'failed', exit_code=code, stopped_at=timestamp())
    _write(folder / ('execution-' + str(number) + '.json'), execution)
    _write(folder / 'execution.json', execution)
    _event(folder, execution['status'], '退出码 ' + str(code))
    return {**status(folder), 'execution_result': execution}


def finish(folder, result):
    folder = Path(folder).resolve()
    if status(folder)['status'] != 'active':
        raise CaseError('Supplementary method is stopped; use its preserved partial results')
    execution = read_json(folder / 'execution.json') if (folder / 'execution.json').exists() else {'status': 'not_started', 'exit_code': None}
    if execution['status'] == 'running':
        raise CaseError('Wait for the registered execution to stop')
    data = read_json(result)
    if not isinstance(data.get('summary'), str) or not data['summary'].strip():
        raise CaseError('Register the result or failure summary')
    if not isinstance(data.get('values', {}), dict) or not isinstance(data.get('assumptions', []), list):
        raise CaseError('Result values and assumptions must be explicit')
    from .core import sha
    _write(folder / 'result.json', data)
    _write(folder / 'finished.json', {'status': 'completed' if execution['exit_code'] == 0 else 'failed',
                                     'stopped_at': timestamp(), 'result_sha256': sha(folder / 'result.json'),
                                     'files': _files(folder)})
    return status(folder)


def values(folder):
    """Read already-registered results; never execute custom code during verification."""
    folder = Path(folder)
    from .core import sha
    done = read_json(folder / 'finished.json')
    if done['status'] != 'completed' or sha(folder / 'result.json') != done['result_sha256']:
        raise CaseError('Only intact, completed supplementary results can supply report quantities')
    return {'values': read_json(folder / 'result.json').get('values', {}),
            'summary': read_json(folder / 'result.json')['summary'],
            'assumptions': read_json(folder / 'result.json').get('assumptions', [])}


if __name__ == '__main__':
    watch(sys.argv[1])
