"""Strict DAT adapter using raw timestamps and the upstream CFG parser.

The upstream DAT reader may reconstruct n/fs across multiple sample-rate segments.
Here timestamp words are preferred; missing timestamps use a cumulative segment clock.
"""
from __future__ import annotations
from array import array
from contextlib import contextmanager
from contextvars import ContextVar
import math
from pathlib import Path
import struct
import warnings
from .vendor.comtrade import Cfg
from .model import Source, Claim, CaseError
from .parsers import scope_for

_decoded_records = ContextVar('powerfault_decoded_records', default=None)

@contextmanager
def decoding_session():
    """Share unchanged decoded records only within one tool operation."""
    if _decoded_records.get() is not None:
        yield
        return
    token = _decoded_records.set({})
    try:
        yield
    finally:
        _decoded_records.reset(token)

def read_cfg(source: Source):
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        cfg = Cfg()
        cfg.read(source.text)
    rates = cfg.sample_rates
    last = 0
    for fs, end in rates:
        if end <= last or fs < 0:
            raise CaseError("CFG sample-rate endpoints must be increasing cumulative sample numbers")
        last = end
    return cfg, [str(w.message) for w in captured]

def decode_dat(cfg, raw: bytes, notes=None):
    analog, status = cfg.analog_count, cfg.status_count
    if cfg.ft == "ASCII":
        for line in raw.decode("ascii").splitlines():
            parts = line.split(",")
            if len(parts) != 2 + analog + status:
                raise CaseError("ASCII DAT column count does not match CFG")
            yield int(parts[0]), int(parts[1]), [float(x) if x else math.nan for x in parts[2:2+analog]], [int(x) for x in parts[2+analog:]]
    else:
        formats = {"BINARY":"h", "BINARY32":"i", "FLOAT32":"f"}
        if cfg.ft not in formats:
            raise CaseError("Unsupported DAT type: " + cfg.ft)
        words = (status + 15) // 16
        layout = struct.Struct("<II" + formats[cfg.ft] * analog + "H" * words)
        padding = 0
        if len(raw) % layout.size:
            padding = len(raw) % layout.size
            declared = cfg.sample_rates[-1][1]
            # Some recorders append a short filler after the last declared sample.
            # Accept it only when the remaining length is exactly the declared
            # sample count and the parsed sample numbers/timestamps stay ordered;
            # a leading header of the same length fails those checks.
            if padding > 16 or len(raw) // layout.size != declared:
                raise CaseError("Binary DAT byte length is not an integral number of CFG-sized records")
            candidate = raw[:-padding]
            rows = list(layout.iter_unpack(candidate))
            numbers = [row[0] for row in rows]
            stamps = [row[1] for row in rows]
            if (not rows or numbers != list(range(numbers[0], numbers[0] + len(rows)))
                    or (any(stamps) and any(b < a for a, b in zip(stamps, stamps[1:])))):
                raise CaseError("Binary DAT byte length is not an integral number of CFG-sized records")
            raw = candidate
            if notes is not None:
                notes.append("Trailing binary DAT padding ignored: " + str(padding) +
                             " bytes outside " + str(declared) + " declared samples")
        for record in layout.iter_unpack(raw):
            packed = record[2+analog:]
            digital = [(packed[i//16] >> (i%16)) & 1 for i in range(status)]
            yield record[0], record[1], record[2:2+analog], digital

def segment_time(n: int, rates) -> float:
    elapsed, start = 0.0, 1
    for rate, end in rates:
        if rate <= 0:
            raise CaseError("Missing timestamp with no positive sampling rate")
        if n <= end:
            return elapsed + (n-start)/rate
        elapsed += (end-start+1)/rate
        start = end+1
    raise CaseError("Sample outside declared rate segments")

def cfg_metadata(source: Source, cfg):
    """Preserve literals, including blanks, rather than upstream default clock codes."""
    lines = source.text.splitlines()
    start = 4 + cfg.analog_count + cfg.status_count + len(cfg.sample_rates)
    def entry(index, column=None, applicable=True):
        if not applicable:
            return {"state": "NOT_DEFINED_IN_REVISION", "raw": None, "evidence": None}
        if index >= len(lines):
            return {"state": "NOT_STATED", "raw": None, "evidence": None}
        line = lines[index]
        parts = line.split(",")
        raw = line if column is None else parts[column] if column < len(parts) else None
        return {"state": "STATED_UNINTERPRETED" if raw and raw.strip() else "NOT_STATED",
                "raw": raw, "evidence": {"source": source.id, "line": index + 1, "quote": line}}
    clock = {"cfg_revision": cfg.rev_year, "nominal_frequency_hz": cfg.frequency,
             "recording_start_literal": entry(start), "trigger_literal": entry(start + 1),
             "timestamp_multiplier": {**entry(start + 3, applicable=cfg.rev_year != "1991"),
                                      "effective_value": cfg.timemult},
             "dat_time_unit_s": cfg.time_base,
             "absolute_time_interpretation": "source_clock_unconverted",
             "synchronization": "NOT_ESTABLISHED", "channel_skew_applied": False}
    for field, index, column in (("time_code", start+4, 0), ("local_code", start+4, 1),
                                 ("time_quality_code", start+5, 0), ("leap_second", start+5, 1)):
        clock[field] = entry(index, column, cfg.rev_year == "2013")
    return clock, [entry(2+i) for i in range(cfg.analog_count)], [
        entry(2+cfg.analog_count+i) for i in range(cfg.status_count)]

def read_record(cfg_source: Source, dat_source: Source, run: Path):
    cache = _decoded_records.get()
    if cache is None:
        return _read_record(cfg_source, dat_source, run)
    path = (run / dat_source.raw_path).resolve()
    stat = path.stat()
    key = (cfg_source.text, str(path), stat.st_size, stat.st_mtime_ns)
    if key not in cache:
        cache[key] = _read_record(cfg_source, dat_source, run)
    return cache[key]

def _read_record(cfg_source: Source, dat_source: Source, run: Path):
    cfg, notes = read_cfg(cfg_source)
    records = list(decode_dat(cfg, (run / dat_source.raw_path).read_bytes(), notes))
    if len(records) != cfg.sample_rates[-1][1]:
        raise CaseError("DAT sample count differs from CFG: " + str(len(records)) +
                        " != " + str(cfg.sample_rates[-1][1]))
    timestamps = [r[1] for r in records]
    signed_origin = None
    if timestamps and timestamps[0] > 0x7FFFFFFF:
        signed = [t if t < 0x80000000 else t - 0x100000000 for t in timestamps]
        trigger = (cfg.trigger_timestamp - cfg.start_timestamp).total_seconds()
        # Some vendors encode a signed, trigger-relative microsecond clock in the
        # nominally unsigned timestamp word. Require independent CFG origin agreement.
        if (all(b > a for a,b in zip(signed,signed[1:])) and
            abs(signed[0] * cfg.time_base * cfg.timemult + trigger) <= 1e-6):
            signed_origin = trigger
            notes.append("Vendor signed trigger-relative DAT clock: origin independently matches CFG")
    use_raw = any(t not in (0, 0xFFFFFFFF) for t in timestamps)
    reconstructed = not use_raw or 0xFFFFFFFF in timestamps
    times, values = array("d"), [array("d") for _ in range(cfg.analog_count)]
    states = [bytearray() for _ in range(cfg.status_count)]
    previous_number = None
    for expected, (n, ts, raw_values, digital) in enumerate(records, 1):
        if n < 1 or (previous_number is not None and n <= previous_number):
            raise CaseError(f"DAT sample numbers must increase: row {expected}, previous {previous_number}, current {n}")
        if previous_number is None and n != 1:
            raise CaseError("DAT initial sample number must be 1; origin is not inferred")
        if signed_origin is not None:
            signed_ts = ts if ts < 0x80000000 else ts - 0x100000000
            t = signed_ts * cfg.time_base * cfg.timemult + signed_origin
        else:
            t = (ts * cfg.time_base * cfg.timemult
             if use_raw and ts != 0xFFFFFFFF else segment_time(n, cfg.sample_rates))
        if not math.isfinite(t) or (times and t <= times[-1]):
            raise CaseError("DAT time axis is not finite and strictly increasing")
        if previous_number is not None and n != previous_number + 1:
            if reconstructed:
                raise CaseError("DAT numbering gap requires explicit timestamps; no missing-time reconstruction")
            rate = next(rate for rate,end in cfg.sample_rates if expected-1 <= end)
            interval = t-times[-1]
            if rate <= 0 or abs(interval/(n-previous_number)-1/rate) > max(2*cfg.time_base*cfg.timemult, .01/rate):
                raise CaseError(f"DAT numbering/time gap disagree: row {expected}, {previous_number}->{n}, interval {interval}")
            notes.append(f"OBSERVED_NUMBERING_GAP: row {expected}, native {previous_number}->{n}; explicit timestamps retained, no samples filled")
        times.append(t)
        previous_number = n
        for i, (raw_value, channel) in enumerate(zip(raw_values, cfg.analog_channels)):
            missing = ((cfg.ft == "BINARY" and raw_value == -32768) or
                       (cfg.ft == "BINARY32" and raw_value == -2147483648) or
                       (cfg.ft == "ASCII" and raw_value == 99999))
            values[i].append(math.nan if missing else raw_value * channel.a + channel.b)
        for i,v in enumerate(digital):
            if v not in (0,1):
                raise CaseError("Digital state must be 0 or 1")
            states[i].append(v)
    if reconstructed:
        notes.append("Missing/all-zero DAT timestamps: cumulative sample-rate reconstruction used")
    basis = ("DAT_signed_trigger_relative_timestamp" if signed_origin is not None
             else "sample_rates_derived" if reconstructed else "DAT_timestamp")
    return cfg, times, values, states, notes, basis

def analyze(cfg_source: Source, dat_source: Source, run: Path, record_id: str):
    cfg, times, values, states, notes, time_basis = read_record(cfg_source, dat_source, run)
    clock, analog_literals, digital_literals = cfg_metadata(cfg_source, cfg)
    short_year = any(len(clock[k]['raw'].split(',')[0].split('/')[-1].strip()) <= 2
                     for k in ('recording_start_literal','trigger_literal'))
    clock['absolute_date_state'] = 'CENTURY_UNRESOLVED' if short_year else 'FOUR_DIGIT_YEAR_STATED'
    trigger = (cfg.trigger_timestamp - cfg.start_timestamp).total_seconds()
    native_numbers = [row[0] for row in decode_dat(cfg, (run/dat_source.raw_path).read_bytes())]
    from .measurement import window_gaps
    gaps = window_gaps(times, 0, len(times)-1, cfg.sample_rates)
    gap_rows = {gap['sample'] for gap in gaps}
    channels, events = [], []
    for i, ch in enumerate(cfg.analog_channels):
        raw_skew = analog_literals[i]["raw"].split(",")[7].strip()
        valid = [x for x in values[i] if math.isfinite(x)]
        if len(valid) != len(values[i]):
            notes.append("MISSING_ANALOG_SAMPLES: channel " + str(ch.n) + ": " + str(len(values[i])-len(valid)))
        channels.append({"number":ch.n, "name":ch.name, "phase":ch.ph, "unit":ch.uu,
                         "circuit":ch.ccbm, "primary":ch.primary if len(analog_literals[i]['raw'].split(','))>=13 else None,
                         "secondary":ch.secondary if len(analog_literals[i]['raw'].split(','))>=13 else None,
                         "side":ch.pors if len(analog_literals[i]['raw'].split(','))>=13 else None, "gain":ch.a, "offset":ch.b,
                         "skew_us":ch.skew if raw_skew else None,
                         "cfg_evidence":analog_literals[i]["evidence"],
                         "sample_count":len(values[i]), "finite_count":len(valid)})
    for i,ch in enumerate(cfg.status_channels):
        for j in range(1,len(times)):
            if states[i][j] != states[i][j-1]:
                events.append({"channel":ch.n,"name":ch.name,"phase":ch.ph,
                               "from":states[i][j-1],"to":states[i][j],
                               "sample":j+1, "time_record_s":times[j],
                               "previous_sample":j, "time_record_interval_s":[times[j-1],times[j]],
                               "native_sample":native_numbers[j], "previous_native_sample":native_numbers[j-1],
                               "edge_observed":j+1 not in gap_rows,
                               "observation":"STATE_DIFFERENCE_ACROSS_GAP" if j+1 in gap_rows else "ADJACENT_SAMPLE_TRANSITION",
                               "interval_bounds":"(]",
                               "time_trigger_ms":(times[j]-trigger)*1000})
    # Keep record clocks separate. Initial state is not emitted as an action.
    events.sort(key=lambda x:(x["sample"],x["channel"]))
    info = {"id":record_id,"cfg_source":cfg_source.id,"dat_source":dat_source.id,
            "station":cfg.station_name,"device":cfg.rec_dev_id,"format":cfg.ft,
            "sample_rates":cfg.sample_rates,"samples":len(times),
            "native_sample_numbers":native_numbers, "time_gaps":gaps,
            "observation_state":"PARTIAL_OBSERVATION" if gaps else "CONTIGUOUS_OBSERVATION",
            "recording_start_time":cfg.start_timestamp.isoformat(timespec="microseconds"),
            "trigger_time":cfg.trigger_timestamp.isoformat(timespec="microseconds"),
            "trigger_offset_s":trigger,"time_basis":time_basis,"clock":clock,"notes":notes,
            "time_range_s":[times[0],times[-1]],"channels":channels,
            "digital_channels":[{"number":c.n,"name":c.name,"phase":c.ph,
                                 "circuit":c.ccbm,
                                 "normal_state":c.y if digital_literals[i]["raw"].split(",")[-1].strip() and
                                     len(digital_literals[i]["raw"].split(",")) >= 5 else None,
                                 "cfg_evidence":digital_literals[i]["evidence"],
                                 "initial_state":states[i][0]}
                                for i,c in enumerate(cfg.status_channels)],
            "events":events}
    claims = []
    if short_year:
        info['recording_start_time']=clock['recording_start_literal']['raw']
        info['trigger_time']=clock['trigger_literal']['raw']
        notes.append('CENTURY_UNRESOLVED: original date literals retained; no century guessed, relative samples remain usable')
    for field,value in (("station",info["station"]),("device",info["device"]),
                        ("recording_start_time",info["recording_start_time"]),("trigger_time",info["trigger_time"])):
        ambiguous = short_year and field in {'recording_start_time','trigger_time'}
        if ambiguous:
            entry=clock['recording_start_literal' if field=='recording_start_time' else 'trigger_literal']
            evidence={'kind':'cfg_literal','field':field,'line':entry['evidence']['line'],'quote':value}
        else:evidence={"kind":"cfg","field":field,"quote":value}
        claims.append(Claim("",field,value,cfg_source.id,scope_for(cfg_source), evidence,
                            status='REVIEW' if ambiguous else 'VERIFIED', reasons=['CENTURY_UNRESOLVED'] if ambiguous else []))
    return info, claims
