"""Deterministic numerical kernels; no model input or sequence interfaces."""
import math
from .model import CaseError

def sine_fit(times, values, frequency):
    """Reorthogonalized QR on three columns, avoiding normal-equation conditioning."""
    if len(times) != len(values) or len(times) < 8:
        raise CaseError("AT_LEAST_EIGHT_PAIRED_SAMPLES_REQUIRED")
    if not math.isfinite(frequency) or frequency <= 0:
        raise CaseError("FREQUENCY_MUST_BE_POSITIVE_FINITE")
    if any(not math.isfinite(x) for x in [*times, *values]):
        raise CaseError("FINITE_SAMPLES_REQUIRED")
    if any(b <= a for a,b in zip(times,times[1:])):
        raise CaseError("STRICTLY_INCREASING_TIME_REQUIRED")
    if times[-1]-times[0] < 1/frequency-1e-10:
        raise CaseError("AT_LEAST_ONE_OBSERVED_CYCLE_REQUIRED")
    if any((b-a)*frequency >= .5-1e-12 for a,b in zip(times,times[1:])):
        raise CaseError("FREQUENCY_NOT_BELOW_LOCAL_NYQUIST")
    phase = [2*math.pi*frequency*(t-times[0]) for t in times]
    columns = [[1.0]*len(times), [math.cos(p) for p in phase], [math.sin(p) for p in phase]]
    q, upper = [], [[0.0]*3 for _ in range(3)]
    for j, column in enumerate(columns):
        vector = column[:]
        for _ in range(2):
            for i, previous in enumerate(q):
                projection = math.fsum(a*b for a,b in zip(previous,vector))
                upper[i][j] += projection
                vector = [a-projection*b for a,b in zip(vector,previous)]
        norm = math.sqrt(math.fsum(x*x for x in vector))
        if norm < math.sqrt(len(times))*1e-8:
            raise CaseError("ILL_CONDITIONED_SINE_FIT")
        upper[j][j] = norm
        q.append([x/norm for x in vector])
    rhs = [math.fsum(a*b for a,b in zip(column,values)) for column in q]
    beta = [0.0]*3
    for j in range(2,-1,-1):
        beta[j] = (rhs[j]-math.fsum(upper[j][i]*beta[i] for i in range(j+1,3)))/upper[j][j]
    dc, cosine, sine = beta
    residual = math.sqrt(math.fsum((value-dc-cosine*c-sine*s)**2
                        for value,c,s in zip(values,columns[1],columns[2]))/len(times))
    return {"dc": dc, "cosine_peak": cosine, "sine_peak": sine,
            "fundamental_rms": math.hypot(cosine,sine)/math.sqrt(2),
            "real_rms": cosine/math.sqrt(2), "imag_rms": -sine/math.sqrt(2),
            "angle_deg": math.degrees(math.atan2(-sine,cosine)), "residual_rms": residual}


def window_gaps(times, first, last, rates):
    gaps = []
    for index in range(first,last):
        rate = next(rate for rate,end in rates if index+1 <= end)
        next_rate = next(rate for rate,end in rates if index+2 <= end)
        if rate <= 0:
            continue  # An irregular declared clock is assessed by actual times in sine_fit.
        observed = times[index+1]-times[index]
        candidates = [r for r in (rate,next_rate) if r>0]
        # At a declared transition, either endpoint clock convention can occur.
        # Actual timestamps remain authoritative; a legal rate change is not missing data.
        if not any(abs(observed-1/r) <= max(2e-6,.01/r) for r in candidates):
            gaps.append({"previous_sample":index+1,"sample":index+2,"observed_s":observed,"expected_s":1/rate})
    return gaps

