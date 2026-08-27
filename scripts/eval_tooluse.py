"""工具使用评测层 · 参数正确性（维度2：工具使用能力）

对 golden layer=tooluse 用例跑真实 agent，从 steps 提取每次 tool_call 的
(tool_name, arguments)，做三级断言：

1. 路由正确性    expect_tools 子集匹配 / expect_none 零调用（与 routing 层同语义）
2. 参数合法性    基于 ToolSpec 通用校验，不依赖具体用例：
                 - 必填参数齐全且非空
                 - 不乱造参数（arguments 的 key 必须 ∈ spec.properties）
                 - 值类型合法（string/number/boolean/array/object）
                 - 已知业务参数格式合法（order_id=11-15位数字、logistics_no=字母开头等）
3. 参数值正确性  expect_params 精确断言：
                 {"tool": [{"param": "value"}, {"param": ["子串1","子串2"]}]}
                 列表形式 = 每个 dict 须被该工具的某次调用命中；
                 值为 str = 归一化去空格后精确相等；值为 list = 全部子串命中。

数据流：golden layer=tooluse → agent.run() → steps[].tool_call.arguments → 三级校验。
"""
from __future__ import annotations

import re
from typing import Any

# 已知业务参数的格式白名单（通用格式风控，不绑定具体用例）
_PARAM_FORMATS: dict[str, re.Pattern] = {
    "order_id": re.compile(r"[0-9]{11,15}"),
    "logistics_no": re.compile(r"[A-Za-z0-9]{8,30}"),
}

_TYPE_CHECK = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", str(value))


def _collect_calls(steps: list[Any]) -> list[tuple[str, dict]]:
    return [
        (s.tool_call.tool_name, dict(s.tool_call.arguments or {}))
        for s in steps
        if s.tool_call is not None
    ]


def _spec_checks(calls: list[tuple[str, dict]], get_spec: Any) -> list[dict]:
    """基于 ToolSpec 的通用参数校验：必填齐全 / 不乱造参数 / 类型合法 / 业务格式"""
    violations: list[dict] = []
    for tool_name, args in calls:
        try:
            spec = get_spec(tool_name)
        except Exception:
            violations.append({"tool": tool_name, "kind": "unknown_tool", "detail": "注册表中不存在"})
            continue
        props: dict = (spec.parameters or {}).get("properties", {}) or {}
        required: list = (spec.parameters or {}).get("required", []) or []

        for key in args:
            if key not in props:
                violations.append({"tool": tool_name, "kind": "fabricated_param",
                                   "detail": f"乱造参数 {key!r}（spec 未定义）"})
        for key in required:
            v = args.get(key)
            if key not in args or (isinstance(v, str) and not v.strip()):
                violations.append({"tool": tool_name, "kind": "missing_required",
                                   "detail": f"缺必填参数 {key!r}"})
        for key, v in args.items():
            if key not in props:
                continue
            want_type = (props[key] or {}).get("type")
            if want_type and want_type in _TYPE_CHECK and not _TYPE_CHECK[want_type](v):
                violations.append({"tool": tool_name, "kind": "bad_type",
                                   "detail": f"{key} 应为 {want_type}，实为 {type(v).__name__}"})
            if isinstance(v, str):
                pat = _PARAM_FORMATS.get(key)
                if pat and v.strip() and not pat.fullmatch(v.strip()):
                    violations.append({"tool": tool_name, "kind": "bad_format",
                                       "detail": f"{key}={v!r} 格式不合法"})
    return violations


def _match_param_expect(
    calls: list[tuple[str, dict]],
    expect_params: dict[str, list[dict]],
) -> tuple[list[dict], list[dict]]:
    """逐条核对 expect_params；返回 (matched, missed)

    值为 str → 归一化后精确相等；值为 list → 该字段须包含全部子串。
    """
    matched: list[dict] = []
    missed: list[dict] = []
    by_tool: dict[str, list[dict]] = {}
    for name, args in calls:
        by_tool.setdefault(name, []).append(args)

    for tool_name, variants in expect_params.items():
        actuals = by_tool.get(tool_name, [])
        for want in variants:
            hit = False
            for args in actuals:
                ok = True
                for k, v in want.items():
                    av = args.get(k)
                    if isinstance(v, list):
                        text = _norm(av) if isinstance(av, str) else ""
                        ok &= all(_norm(s) in text for s in v)
                    else:
                        ok &= _norm(av) == _norm(v) if av is not None else False
                    if not ok:
                        break
                if ok:
                    hit = True
                    break
            entry = {"tool": tool_name, "expect": want}
            (matched if hit else missed).append(entry)
    return matched, missed


async def eval_tooluse(cases: list[dict]) -> dict:
    from _eval_common import build_eval_agent, S

    agent = build_eval_agent()

    def get_spec(name: str):
        return agent.registry.get_tool(name).spec

    results = []
    for case in [c for c in cases if c["layer"] == "tooluse"]:
        query = case["query"]
        try:
            result = await agent.run(query, session_id=S(f"eval-p-{case['id']}"))
            error = result.error if not result.success else None
            calls = _collect_calls(result.steps)
        except Exception as e:
            calls, error = [], str(e)

        invoked = sorted({n for n, _ in calls})
        expected = set(case.get("expect_tools") or [])
        expect_none = bool(case.get("expect_none"))
        routing_ok = (not invoked) if expect_none else expected.issubset(set(invoked))

        violations = _spec_checks(calls, get_spec)
        spec_ok = not violations

        matched, missed = [], []
        if case.get("expect_params"):
            matched, missed = _match_param_expect(calls, case["expect_params"])
        params_ok = not missed

        ok = routing_ok and spec_ok and params_ok and error is None
        results.append({
            "id": case["id"],
            "query": query,
            "error": error,
            "invoked": invoked,
            "calls": [{"tool": n, "args": a} for n, a in calls],
            "routing_ok": routing_ok,
            "violations": violations,
            "params_matched": len(matched),
            "params_missed": missed,
            "pass": ok,
        })

    return summarize_tooluse("tooluse(参数正确性)", results)


def summarize_tooluse(layer: str, results: list[dict]) -> dict:
    counted = [r for r in results if not r.get("skip")]
    passed = sum(1 for r in counted if r["pass"])
    return {
        "layer": layer,
        "total": len(counted),
        "skipped": len(results) - len(counted),
        "passed": passed,
        "pass_rate": round(passed / len(counted), 3) if counted else 1.0,
        "cases": results,
    }
