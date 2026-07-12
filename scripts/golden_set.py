"""Golden set CLI (Phase 0B).

  python scripts/golden_set.py mine   [--target 120] [--out artifacts/golden/candidates.json]
  python scripts/golden_set.py freeze --version v1
         [--candidates artifacts/golden/candidates.json] [--outdir artifacts/golden]
  python scripts/golden_set.py eval   --golden artifacts/golden/golden_v1.json
         --label baseline [--prompt FILE] [--baseline artifacts/golden/report_baseline.json]

mine   — кандидаты из закрытых тикетов HDE (нужны HDE_API_* в .env).
freeze — валидация + неизменяемый снапшот + golden_tickets.txt.
eval   — прогон промпта (по умолчанию active/встроенный) по golden set,
         отчёт в artifacts/golden/report_<label>.json; с --baseline печатает gate.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GOLDEN_DIR = Path("artifacts/golden")


async def cmd_mine(args: argparse.Namespace) -> None:
    from bot.config import config
    from bot.hde_api import HDEApiClient
    from bot.optimizer.golden import mine_candidates

    client = HDEApiClient()
    cases = await mine_candidates(client, config.hde_owner_id, target=args.target)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Собрано {len(cases)} кандидатов -> {out}")
    print("Проверь кейсы вручную (expected_action, слабые — удалить), затем freeze.")


def cmd_freeze(args: argparse.Namespace) -> None:
    from bot.optimizer.golden import freeze_golden, golden_ticket_ids

    cases = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    golden = freeze_golden(cases, version=args.version)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    golden_path = outdir / f"golden_{args.version}.json"
    golden_path.write_text(
        json.dumps(golden, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tickets_path = outdir / "golden_tickets.txt"
    tickets_path.write_text(
        "\n".join(sorted(golden_ticket_ids(golden))) + "\n", encoding="utf-8"
    )
    print(f"Заморожено {len(cases)} кейсов -> {golden_path}")
    print(f"Список тикетов для исключения из retrieval -> {tickets_path}")


async def cmd_eval(args: argparse.Namespace) -> None:
    from bot.optimizer.golden import compare_to_baseline, evaluate_golden, load_golden

    golden = load_golden(args.golden)
    if args.prompt:
        prompt = Path(args.prompt).read_text(encoding="utf-8")
    else:
        from bot.ai_summary import _FORMAT_INSTRUCTIONS
        prompt = _FORMAT_INSTRUCTIONS
    print(f"Golden {golden['version']}: {len(golden['cases'])} кейсов, "
          f"label={args.label}, model={args.model or 'config'}, "
          f"reasoning_effort={args.reasoning_effort!r}")
    gen_fn = None
    if args.model is not None or args.reasoning_effort is not None:
        from bot.optimizer.evaluator import _generate_answer

        async def gen_fn(history, title, instructions):
            return await _generate_answer(
                history, title, instructions,
                model=args.model, reasoning_effort=args.reasoning_effort,
            )
    report = await evaluate_golden(
        golden["cases"], prompt, label=args.label, _generate_fn=gen_fn,
        pace_s=args.pace,
    )
    out = GOLDEN_DIR / f"report_{args.label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    agg = report["aggregates"]
    print(f"Отчёт -> {out}")
    print(f"  judged {agg['judged']}/{agg['cases_total']} "
          f"(judge_failed {agg['judge_failed']}, "
          f"generation_failed {agg.get('generation_failed', 0)})")
    print(f"  action_accuracy={agg['action_accuracy']} "
          f"unsupported={agg['mean_unsupported']} "
          f"correctness={agg['mean_correctness']} "
          f"usefulness={agg['mean_usefulness']} "
          f"safety_violations={agg['safety_violations']}")
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        gate = compare_to_baseline(baseline, report)
        print(f"\nGATE: {'PASSED' if gate['passed'] else 'FAILED'}")
        for check in gate["checks"]:
            mark = "✅" if check["passed"] else "❌"
            print(f"  {mark} {check['name']}: baseline={check['baseline']} "
                  f"candidate={check['candidate']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_mine = sub.add_parser("mine", help="кандидаты из закрытых тикетов HDE")
    p_mine.add_argument("--target", type=int, default=120)
    p_mine.add_argument("--out", default=str(GOLDEN_DIR / "candidates.json"))

    p_freeze = sub.add_parser("freeze", help="заморозить валидированные кейсы")
    p_freeze.add_argument("--version", required=True)
    p_freeze.add_argument("--candidates", default=str(GOLDEN_DIR / "candidates.json"))
    p_freeze.add_argument("--outdir", default=str(GOLDEN_DIR))

    p_eval = sub.add_parser("eval", help="оценить промпт на golden set")
    p_eval.add_argument("--golden", required=True)
    p_eval.add_argument("--label", required=True)
    p_eval.add_argument("--prompt", default=None,
                        help="файл промпта; по умолчанию _FORMAT_INSTRUCTIONS")
    p_eval.add_argument("--baseline", default=None,
                        help="report-файл baseline для gate-сравнения")
    p_eval.add_argument("--model", default=None,
                        help="модель генерации (по умолчанию config.groq_summary_model)")
    p_eval.add_argument("--reasoning-effort", dest="reasoning_effort", default=None,
                        help="none — отключить <think> для reasoning-моделей; "
                             "'' — не передавать (для llama)")
    p_eval.add_argument("--pace", type=float, default=0.0,
                        help="пауза сек между кейсами (троттлинг под free-tier TPM)")

    args = parser.parse_args()
    if args.command == "mine":
        asyncio.run(cmd_mine(args))
    elif args.command == "freeze":
        cmd_freeze(args)
    elif args.command == "eval":
        asyncio.run(cmd_eval(args))


if __name__ == "__main__":
    main()
