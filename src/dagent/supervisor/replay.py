"""supervisor-replay: re-run a saved packet (data/<task_id>/packets/<seq>.json,
written by supervisor/llm.py on every invocation) against the CURRENT
supervisor prompt/model. This exists so heuristic changes get judged against
real saved packets rather than vibes, without paying for a live run.

    dagent-supervisor-replay data/<task_id>/packets/<seq>.json [--model <model>]
"""
import argparse
import asyncio
import json

from dagent.supervisor.llm import invoke_supervisor
from dagent.supervisor.schema import TriagePacket


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="dagent-supervisor-replay")
    p.add_argument("packet_path")
    p.add_argument("--model")
    args = p.parse_args(argv)

    saved = json.loads(open(args.packet_path).read())
    packet = TriagePacket.model_validate(saved["packet"])

    result = asyncio.run(invoke_supervisor(packet, model=args.model))

    print("ORIGINAL:", json.dumps(saved["action"], indent=2))
    print("REPLAYED:", result.action.model_dump_json(indent=2))
    changed = saved["action"] != result.action.model_dump()
    print("changed:", changed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
