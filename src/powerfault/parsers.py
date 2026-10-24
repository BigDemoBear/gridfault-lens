"""Content-specific adapters. Structured HDR is XML, irrespective of extension."""
from __future__ import annotations
import configparser
from datetime import datetime
from pathlib import PurePosixPath
import re
import xml.etree.ElementTree as ET
from .model import Claim, Source, Issue, CaseError, finite_number

FIELD_LABELS = {
    "厂站名称": "station", "变电站名称": "station", "电站名称": "station",
    "一次设备名称": "line", "故障设备名称": "line", "故障线路": "line",
    "故障相别": "fault_phase", "故障测距": "fault_distance", "故障距离": "fault_distance",
    "线路总长度": "line_length", "故障开始时间": "fault_start_offset",
    "故障结束时间": "fault_end_offset",
    "重合闸结果": "reclose_status", "故障零时刻": "fault_time",
    "保护启动时间": "protection_start_time", "故障测距结果": "fault_distance",
    "重合闸动作时间": "reclose_time", "故障性质": "device_fault_nature",
    "CT一次额定值": "ct_primary", "CT二次额定值": "ct_secondary",
    "PT一次线电压": "pt_primary", "PT二次线电压": "pt_secondary",
}
PARAMETERS = {"ct_primary", "ct_secondary", "pt_primary", "pt_secondary"}
ALLOWED = set(FIELD_LABELS.values()) | {
    "recording_start_time", "trigger_time", "device", "grounded", "protection_event", "device_result", "native_measurement",
    "reclose_time", "breaker_open_time", "breaker_close_time", "fault_duration",
}
TIMES = {"fault_time", "trigger_time", "recording_start_time", "protection_start_time"}
NUMBERS = {"fault_distance", "line_length", "fault_start_offset", "fault_end_offset",
           "reclose_time", "breaker_open_time", "breaker_close_time", "fault_duration"}
NUMBER = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*([^\d\s].*?)?\s*$")
PHASES = {"A","B","C","AB","BC","CA","ABC"}
MEASUREMENT_LABEL = re.compile(r"^([ABCN])相(故障前|故障)(电压|电流)$")
MEASUREMENT_EXTREMA = {"最小故障电压": ("voltage", "minimum_reported"),
                       "最大故障电流": ("current", "maximum_reported"),
                       "最大零序电流": ("zero_sequence_current", "maximum_reported"),
                       "最大差动电流": ("differential_current", "maximum_reported")}
SCALAR = r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
MEASUREMENT_VALUE = re.compile(r"\s*("+SCALAR+r")(?:\s*∠\s*("+SCALAR+r")\s*°)?\s*")

def measurement_label(label):
    match = MEASUREMENT_LABEL.fullmatch(label or "")
    if match:
        return {"phase": match[1], "stage": "pre_fault" if match[2] == "故障前" else "fault",
                "quantity": "voltage" if match[3] == "电压" else "current", "statistic": "reported"}
    if label in MEASUREMENT_EXTREMA:
        quantity, statistic = MEASUREMENT_EXTREMA[label]
        return {"phase": None, "stage": "fault", "quantity": quantity, "statistic": statistic}
    return None

def reported_measurement(label, raw, unit):
    description = measurement_label(label)
    match = MEASUREMENT_VALUE.fullmatch(raw or "")
    if not match:
        raise CaseError("Unsupported native measurement literal: " + str(raw))
    magnitude = finite_number(match[1])
    angle = finite_number(match[2]) if match[2] is not None else None
    return {**description, "label": label, "raw_value": raw, "raw_unit": unit,
            "magnitude": magnitude, "angle_deg": angle,
            "magnitude_definition": "NOT_STATED", "angle_reference": "NOT_STATED",
            "side": "NOT_STATED", "window": "NOT_STATED", "zero_sequence_multiple": "NOT_STATED"}

def normalize(field: str, raw: str, unit: str | None = None):
    raw = raw.strip()
    if field in PARAMETERS:
        match = NUMBER.fullmatch(raw)
        if not match:
            raise CaseError("Not an explicit parameter value: " + raw)
        number, unit = finite_number(match[1]), match[2] or unit
        # Preserve impedance/angle conventions and absent units; no guessed per-km basis.
        scales = {"A": 1, "kA": 1000, "mA": .001} if field.startswith("ct_") else (
            {"V": 1, "kV": 1000} if field.startswith("pt_") else {})
        if scales and unit is not None:
            if unit not in scales:
                raise CaseError("Unrecognized parameter unit: " + unit)
            number, unit = number * scales[unit], "A" if field.startswith("ct_") else "V"
        return number, unit
    if field == "fault_phase":
        phase = raw.upper().replace("相", "").replace(" ", "").replace("接地", "N")
        phase = phase.replace("G", "N")
        phase = phase.removesuffix("N")
        if phase == "AC":
            phase = "CA"
        if phase not in PHASES:
            raise CaseError("Unrecognized fault phase: " + raw)
        return phase, None
    if field in NUMBERS:
        match = NUMBER.fullmatch(raw)
        if not match:
            raise CaseError("Not an explicit numeric value: " + raw)
        n, u = finite_number(match[1]), (match[2] or unit)
        if field in {"fault_distance", "line_length"}:
            if u and u.lower() in {"km", "公里", "千米"}:
                u = "km"
            elif u and u.lower() in {"m", "米"}:
                n, u = n / 1000, "km"
            elif u:
                raise CaseError("Unrecognized distance unit: " + u)
            # No unit is invented for a unitless source.
        elif u and u.lower() in {"ms", "毫秒"}:
            u = "ms"
        elif u and u.lower() in {"s", "秒"}:
            n, u = n * 1000, "ms"
        elif u:
            raise CaseError("Unrecognized time-offset unit: " + u)
        return n, u
    if field in TIMES:
        # Date order for INF/CFG is supplied by their dedicated adapter.
        return datetime.fromisoformat(raw).isoformat(timespec="microseconds"), None
    if field == "grounded":
        if raw.lower() not in {"true", "false", "1", "0"}:
            raise CaseError("Unrecognized boolean: " + raw)
        return raw.lower() in {"true", "1"}, None
    if field == "reclose_status":
        mapping = {"成功":"success",
                   "失败":"failure", "不成功":"failure", "未动作":"not_operated",
                   "闭锁":"blocked", "success":"success", "failure":"failure",
                   "blocked":"blocked", "not_operated":"not_operated"}
        if raw.lower() not in mapping:
            raise CaseError("Unrecognized reclose status: " + raw)
        return mapping[raw.lower()], None
    if not raw:
        raise CaseError("Empty literal")
    return raw, unit

def scope_for(source: Source) -> str:
    return str(PurePosixPath(source.path).with_suffix("")).casefold()

def xml_nodes(root):
    def walk(node, path):
        yield path, node
        counts = {}
        for child in node:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            yield from walk(child, path + "/" + child.tag + "[" + str(counts[child.tag]) + "]")
    yield from walk(root, "/" + root.tag)

def resolve_xml(text: str, path: str):
    root = ET.fromstring(text)
    return next((n for p, n in xml_nodes(root) if p == path), None)


def native_groups(root):
    """Address native blocks, without assigning an electrical stage to them.

    The flat FaultReport dialect terminates a report block with FaultStartTime.
    RecorderReport supplies explicit Fault parents. Keep both original paths and
    source time literals; neither convention establishes an event time zero.
    """
    nodes = list(xml_nodes(root))
    groups = {}
    if root.tag == 'FaultReport':
        pending = []
        for path, node in nodes:
            if path.count('/') != 2:
                continue
            if node.tag in {'FaultInfo', 'TripInfo', 'DigitalEvent', 'FaultKeepingTime', 'FaultStartTime'}:
                pending.append(path)
            if node.tag == 'FaultStartTime':
                group = {'path': path, 'time_literal': node.text or node.get('value')}
                for item in pending:
                    groups[item] = group
                pending = []
    else:
        for path, node in nodes:
            if node.tag == 'Fault':
                time = node.find('FaultTime')
                group = {'path': path, 'time_literal': None if time is None else time.get('value') or time.text}
                for child_path, _ in nodes:
                    if child_path == path or child_path.startswith(path + '/'):
                        groups[child_path] = group
    return groups

def parse_source(source: Source) -> tuple[list[Claim], list[Issue]]:
    if source.text is None:
        return [], []
    claims, issues = [], []
    scope = scope_for(source)
    def emit(field, raw, evidence, unit=None, basis=None, claim_scope=None):
        if raw is None or not str(raw).strip():
            return
        try:
            value, unit = normalize(field, str(raw), unit)
        except (CaseError, ValueError) as exc:
            issues.append(Issue("INVALID_FIELD", field + ": " + str(exc), source.id))
            return
        reasons = []
        if field in TIMES and str(value).startswith("2000-01-01"):
            reasons.append("POSSIBLE_DEVICE_SENTINEL_DATE")
        claims.append(Claim("", field, value, source.id, claim_scope or scope, evidence,
                            unit=unit, basis=basis,
                            status="REVIEW" if reasons else "VERIFIED", reasons=reasons))

    text = source.text
    if text.lstrip().startswith("<"):
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            return [], [Issue("XML_PARSE_ERROR", str(exc), source.id)]
        if root.tag not in {"FaultReport", "RecorderReport"}:
            return [], [Issue("UNKNOWN_XML_FORMAT", root.tag, source.id)]
        nodes = list(xml_nodes(root))
        fault_paths = [p for p,n in nodes if n.tag == "Fault"]
        def fault_scope(path):
            for i,p in enumerate(fault_paths):
                if path == p or path.startswith(p + "/"):
                    return scope + "#fault:" + str(i + 1)
            return scope
        for path, node in nodes:
            ev = {"kind":"xml", "path":path, "quote":ET.tostring(node, encoding="unicode")}
            if root.tag == "FaultReport":
                name, val = node.findtext("name"), node.findtext("value")
                if node.tag in {"DeviceInfo","FaultInfo","SettingValue"} and name in FIELD_LABELS:
                    emit(FIELD_LABELS[name], val, ev, node.findtext("unit"))
                elif node.tag == "FaultInfo" and measurement_label(name) and val and val.strip():
                    unit = node.findtext("unit")
                    try:
                        measurement = reported_measurement(name, val, unit)
                    except CaseError as exc:
                        issues.append(Issue("INVALID_FIELD", "native_measurement: " + str(exc), source.id))
                        continue
                    reasons = ["DEVICE_MEASUREMENT_CONTEXT_NOT_STATED"]
                    expected = {"V", "kV", "mV"} if measurement["quantity"] == "voltage" else {"A", "kA", "mA"}
                    if unit not in expected:
                        reasons.append("MEASUREMENT_UNIT_UNKNOWN")
                    if measurement["magnitude"] < 0:
                        reasons.append("NEGATIVE_MAGNITUDE")
                    claims.append(Claim("", "native_measurement", measurement, source.id, scope, ev,
                                        unit=unit, basis="device_reported", status="REVIEW", reasons=reasons))
                elif node.tag in {"TripInfo","DigitalEvent"} and node.find("name") is not None:
                    event = {"name":name, "raw_time":node.findtext("time"),
                             "state":val, "phase":node.findtext("phase")}
                    time_text = event["raw_time"]
                    if time_text:
                        try:
                            event["time"], event["unit"] = normalize("fault_start_offset", time_text, node.findtext("unit"))
                        except (CaseError, ValueError):
                            issues.append(Issue("EVENT_TIME_UNPARSED", time_text, source.id))
                    event["time_basis"] = node.findtext("basis") or "source_relative_unspecified"
                    reasons = ["EVENT_TIME_UNIT_NOT_STATED"] if time_text and not event.get("unit") else []
                    claims.append(Claim("", "protection_event", event, source.id, scope, ev,
                                        status="REVIEW" if reasons else "VERIFIED", reasons=reasons))
                elif node.tag == "FaultStartTime":
                    occurrence = int(re.search(r"FaultStartTime\[(\d+)\]$", path)[1])
                    emit("fault_time", node.text or node.get("value"), ev,
                         claim_scope=scope + "#fault:" + str(occurrence))
            else:
                sc = fault_scope(path)
                attrs = {"SubStation":"station", "Recorder":"device", "Bay":"line",
                         "StartTime":"recording_start_time", "TriggerTime":"trigger_time",
                         "FaultTime":"fault_time", "Position":"fault_distance",
                         "Grounded":"grounded", "Result":"device_result", "RecloseResult":"reclose_status",
                         "Duration":"fault_duration", "CHZTime":"reclose_time",
                         "BreakerJumpTime":"breaker_open_time", "BreakerShutTime":"breaker_close_time"}
                if node.tag in attrs:
                    field = attrs[node.tag]
                    emit(field, node.get("value"), ev, unit=node.get("unit"),
                         basis=node.get("basis"), claim_scope=sc)
                elif node.tag == "Phase" and "/FPT[" in path:
                    emit("fault_phase", node.get("value"), ev, claim_scope=sc)
                elif re.search(r'/(CycBefort|Cyc1After|Cyc2After)\[\d+\]/[UI][abc]\[\d+\]$', path) and node.get('value'):
                    parent_path = path.rsplit('/', 1)[0]
                    parent = next(n for p,n in nodes if p == parent_path)
                    before = parent.tag == 'CycBefort'
                    phase = node.tag[-1].upper()
                    label = phase + '相' + ('故障前' if before else '故障') + ('电压' if node.tag[0] == 'U' else '电流')
                    try:
                        value = reported_measurement(label, node.get('value'), node.get('unit'))
                        value['window'] = parent.get('desc') or parent.tag
                        if node.get('angle'):
                            value['angle_deg'] = finite_number(node.get('angle'))
                    except (CaseError, ValueError) as exc:
                        issues.append(Issue('INVALID_FIELD', 'native_measurement: ' + str(exc), source.id))
                        continue
                    claims.append(Claim('', 'native_measurement', value, source.id, sc, ev,
                                        unit=node.get('unit'), basis='device_reported', status='REVIEW',
                                        reasons=['DEVICE_MEASUREMENT_CONTEXT_NOT_STATED']))
                elif node.tag == "Digital":
                    for j,act in enumerate(node.findall("DigitalAct"), 1):
                        event = {"name":node.get("Name"), "raw_time":act.get("Time"),
                                 "state":act.get("State"), "channel":node.get("DigitalChnNo"),
                                 "sample_index":act.get("SampleNum"),
                                 "time_basis":act.get("basis") or node.get("basis") or "source_relative_unspecified",
                                 "time":None, "unit":act.get("unit") or node.get("unit")}
                        if event["raw_time"]:
                            try:
                                event["time"], event["unit"] = normalize("fault_start_offset", event["raw_time"], event["unit"])
                            except (CaseError, ValueError):
                                issues.append(Issue("EVENT_TIME_UNPARSED", event["raw_time"], source.id))
                        reasons = ["EVENT_TIME_UNIT_NOT_STATED"] if event["raw_time"] and not event["unit"] else []
                        claims.append(Claim("", "protection_event", event, source.id,
                                            sc, ev, status="REVIEW" if reasons else "VERIFIED", reasons=reasons))
        groups = native_groups(root)
        for claim in claims:
            path = claim.evidence.get('path', '')
            group = next((g for p,g in groups.items() if path == p or path.startswith(p + '/')), None)
            if group:
                claim.source_scope = source.path + '#' + group['path']
                claim.evidence['group'] = group
        return claims, issues

    if source.kind == "inf":
        ini = configparser.ConfigParser(interpolation=None, strict=True)
        ini.optionxform = str
        try:
            ini.read_string(text)
        except configparser.Error as exc:
            return [], [Issue("INF_PARSE_ERROR", str(exc), source.id)]
        fields = {"Station_Name":"station", "Recording_Device_ID":"device",
                  "File_Start_Time":"recording_start_time", "Trigger_Time":"trigger_time"}
        section = "Public File_Description"
        if ini.has_section(section):
            for key, field in fields.items():
                if not ini.has_option(section, key):
                    continue
                raw = ini.get(section, key)
                ev = {"kind":"ini","section":section,"key":key,"quote":raw}
                if field in TIMES and raw:
                    try:
                        raw = datetime.strptime(raw,"%d/%m/%Y,%H:%M:%S.%f").isoformat()
                    except ValueError:
                        issues.append(Issue("INF_TIME_UNPARSED", key + "=" + raw, source.id))
                        continue
                emit(field,raw,ev)
        return claims, issues

    # Free text rules are deliberately label-based, never arbitrary phase mentions.
    pattern = re.compile(r"(?m)^\s*(" + "|".join(map(re.escape, FIELD_LABELS)) + r")\s*[:：]\s*([^\n]+)")
    for m in pattern.finditer(text):
        raw = m[2].strip()
        start = m.start(2) + len(m[2]) - len(m[2].lstrip())
        ev = {"kind":"text","start":start,"end":start+len(raw),"quote":raw}
        emit(FIELD_LABELS[m[1]],raw,ev)
    if source.kind not in {"cfg","dat"}:
        issues.append(Issue("AI_REVIEW_REQUIRED",
                            "Free text needs field coverage and semantic review; rules are not exhaustive", source.id))
    return claims, issues
