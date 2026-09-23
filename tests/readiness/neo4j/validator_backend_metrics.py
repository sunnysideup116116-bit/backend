"""Backend-selection development gates; frozen semantic map, stricter reliability."""
from r34_metrics import r34_metrics


def backend_metrics(jobs,cases):
    result=r34_metrics(jobs,cases)
    gates=dict(result["blocking_gates"])
    gates["first_pass_validity"]=result["first_valid"]*200>=len(jobs)*199
    gates["eventual_validity"]=result["eventual_valid"]==len(jobs)
    result.update(blocking_gates=gates,PASS=all(gates.values()),
        scope="Backend selection: original96 development only; no retired final reuse")
    return result
