"""4c1c9be P2-4：病态输入一律 4xx（400/422），绝不 500。

覆盖：请求体 lone surrogate（typed str 与 asr 容器的键/值）、非法 UTF-8
字节、asr 超深嵌套（schema 深度限制 + 解析期 RecursionError 兜底）、
超大整数（PATCH rev、sync since、notes cursor——SQL 绑定 OverflowError）、
极端日期（aware 换算 UTC 越界、naive 本地化越界）。path/query NUL 的
回归在 test_nul.py（中间件层）。
"""
import uuid

from tests.conftest import auth_headers

_4XX = (400, 422)


def _raw(content: bytes, headers: dict) -> dict:
    return {"content": content, "headers": {**headers, "content-type": "application/json"}}


def _deep_asr_body(depth: int) -> bytes:
    """手工拼超深嵌套的 JSON（json.dumps 本身也会 RecursionError）。"""
    return (
        b'{"client_note_id": "deep", "text": "hi", "asr": '
        + b'{"a":' * depth
        + b"1"
        + b"}" * depth
        + b"}"
    )


async def test_lone_surrogate_in_body_rejected_422(client, user):
    """lone surrogate 编码不成 UTF-8，入库 UnicodeEncodeError → 500：
    typed str 字段（pydantic string_unicode）与 asr 容器的键/值（递归
    校验）都必须拦成 422。"""
    headers = auth_headers(user)
    cases = [
        b'{"client_note_id": "sc1", "text": "a\\ud800b"}',  # text 字段
        b'{"client_note_id": "sc2", "text": "ok", "asr": {"k": "a\\udfffb"}}',  # asr 值
        b'{"client_note_id": "sc3", "text": "ok", "asr": {"a\\ud800": 1}}',  # asr 键
    ]
    for raw in cases:
        resp = await client.post("/v1/notes", **_raw(raw, headers))
        assert resp.status_code in _4XX, (raw, resp.status_code, resp.text)
    # ask 的 text 同理
    resp = await client.post("/v1/ask", **_raw(b'{"text": "\\ud800"}', headers))
    assert resp.status_code in _4XX, resp.text


async def test_invalid_utf8_body_rejected_4xx(client, user):
    """请求体含非法 UTF-8 字节：解码失败 → 400/422，不是 500。"""
    resp = await client.post(
        "/v1/notes", **_raw(b'{"client_note_id": "uf", "text": "\xff\xfe"}', auth_headers(user))
    )
    assert resp.status_code in _4XX, (resp.status_code, resp.text)


async def test_deeply_nested_asr_over_depth_limit_422(client, user):
    """asr 嵌套深度受限（_MAX_ASR_DEPTH=16）：超限 422；限内正常创建。"""
    headers = auth_headers(user)
    ok = await client.post("/v1/notes", **_raw(_deep_asr_body(16), headers))
    assert ok.status_code == 202, (ok.status_code, ok.text)

    over = await client.post("/v1/notes", **_raw(_deep_asr_body(50), headers))
    assert over.status_code in _4XX, (over.status_code, over.text)


async def test_absurdly_deep_body_recursion_guard_422(client, user):
    """超深嵌套要么被 schema 深度限制拦下（422），要么在 json 解析阶段
    炸递归栈——由 FastAPI 的解析包装（400）或 RecursionError 处理器
    （422）兜住。无论落在哪层都不是 500。"""
    for depth in (3000, 50000):
        resp = await client.post("/v1/notes", **_raw(_deep_asr_body(depth), auth_headers(user)))
        assert resp.status_code in _4XX, (depth, resp.status_code, resp.text)


async def test_huge_int_inputs_422(client, user):
    """超大整数绑到 SQL 列（int4/int8）会 OverflowError → 500：Pydantic/
    Query 约束拦成 422。"""
    headers = auth_headers(user)
    created = await client.post(
        "/v1/notes", json={"client_note_id": str(uuid.uuid4()), "text": "hi"}, headers=headers
    )
    assert created.status_code == 202, created.text
    note_id = created.json()["note_id"]

    patched = await client.patch(
        f"/v1/notes/{note_id}", json={"text": "x", "rev": 10**30}, headers=headers
    )
    assert patched.status_code in _4XX, (patched.status_code, patched.text)

    synced = await client.get("/v1/sync", params={"since": 10**20}, headers=headers)
    assert synced.status_code in _4XX, (synced.status_code, synced.text)

    listed = await client.get("/v1/notes", params={"cursor": 10**20}, headers=headers)
    assert listed.status_code in _4XX, (listed.status_code, listed.text)


async def test_extreme_datetime_inputs_422(client, user):
    """极端日期换算/本地化成 UTC 越界（astimezone OverflowError → 500）：
    SafeDatetime 校验拦成 422；边界内的 aware 极值正常接受（aware/naive
    统一安全界：换算/视为 UTC 后在 0001-01-02–9998-12-31，threads5 P2-4）。"""
    headers = auth_headers(user)
    rejected = [
        # aware：换算 UTC 后越过统一安全界（9998 年上界，预留 ±14h 偏移）
        "9999-12-31T23:59:59.999999-05:00",
        "9999-12-31T23:59:59+05:00",
        "0001-01-01T00:00:00+05:00",
        # naive：按最坏 IANA 偏移（±14h）本地化后越界
        "0001-01-01T00:00:00",
        "9999-12-31T23:59:59",
    ]
    for i, dt in enumerate(rejected):
        resp = await client.post(
            "/v1/notes",
            json={"client_note_id": f"dt{i}", "text": "hi", "recorded_at": dt},
            headers=headers,
        )
        assert resp.status_code in _4XX, (dt, resp.status_code, resp.text)

    # ask 的 client_time 在 schema 层同样被拦（不依赖是否命中记忆）
    asked = await client.post(
        "/v1/ask",
        json={"text": "hi", "client_time": "9999-12-31T23:59:59.999999-12:00"},
        headers=headers,
    )
    assert asked.status_code in _4XX, (asked.status_code, asked.text)

    # 边界内的 aware 极值（换算 UTC 后仍在 0002–9998 年）必须照常接受
    ok = await client.post(
        "/v1/notes",
        json={
            "client_note_id": "dtok",
            "text": "hi",
            "recorded_at": "9998-12-31T20:00:00+05:00",
        },
        headers=headers,
    )
    assert ok.status_code == 202, (ok.status_code, ok.text)


async def test_aware_datetime_boundary_rejected_before_extraction_no_500(client, user, run_jobs):
    """threads5 P2-4 / INV-7：`9999-12-31T23:59:59Z` 是合法 UTC 值，但配
    用户时区 Asia/Shanghai 后，抽取/提醒解析的 astimezone(+8h) 会
    OverflowError——旧校验只试「能转 UTC」放它过 schema，note 被标 failed；
    现在 aware/naive 统一安全界，schema 层 422（绝不是 500/failed）。边界
    内的 aware 值带着时区走完整抽取链路不炸。"""
    headers = auth_headers(user)

    rejected = await client.post(
        "/v1/notes",
        json={
            "client_note_id": "aware_bad",
            "text": "明天早上八点提醒我开会",
            "recorded_at": "9999-12-31T23:59:59Z",
            "timezone": "Asia/Shanghai",
        },
        headers=headers,
    )
    assert rejected.status_code in _4XX, (rejected.status_code, rejected.text)
    assert rejected.json()["error"]["code"] == "validation_error"

    ok = await client.post(
        "/v1/notes",
        json={
            "client_note_id": "aware_ok",
            "text": "明天早上八点提醒我开会",
            "recorded_at": "9998-12-31T23:00:00Z",
            "timezone": "Asia/Shanghai",
        },
        headers=headers,
    )
    assert ok.status_code == 202, (ok.status_code, ok.text)
    await run_jobs()
    note_id = ok.json()["note_id"]
    detail = await client.get(f"/v1/notes/{note_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["status"] == "organized", (
        f"边界内 aware 时间应走完抽取不炸：{detail.json()['status']}"
    )


async def test_asr_size_measured_in_utf8_bytes_cjk_rejected(client, user):
    """threads12 P2（schemas.py:199）/ INV-7：asr 体积上限按 **UTF-8 字节**
    量测。旧行为 len(json.dumps(...)) 数码点——约 1400 个 CJK 字符的 asr
    只有 ~1400 个码点却占 ~4.2KB 字节，落库/日志的实际负担翻三倍不被
    拦截。对照：接近上限但按字节仍在界内的 ASCII asr 正常 202。"""
    import json as jsonlib

    headers = auth_headers(user)

    # 1400 个 CJK 字符：码点数 < 4096，但 UTF-8 字节数（含 JSON 括号）> 4096
    cjk_text = "超" * 1400
    assert len(jsonlib.dumps({"v": cjk_text}, ensure_ascii=False)) < 4096
    assert len(jsonlib.dumps({"v": cjk_text}, ensure_ascii=False).encode("utf-8")) > 4096
    over = await client.post(
        "/v1/notes",
        content=jsonlib.dumps(
            {"client_note_id": str(uuid.uuid4()), "text": "hi", "asr": {"v": cjk_text}}
        ).encode("utf-8"),
        headers={**headers, "content-type": "application/json"},
    )
    assert over.status_code in _4XX, (over.status_code, over.text)

    # 边界对照：接近 4096 字节的纯 ASCII asr 正常通过
    ascii_pad = "a" * 4060
    assert len(jsonlib.dumps({"v": ascii_pad}).encode("utf-8")) > 4000
    ok = await client.post(
        "/v1/notes",
        content=jsonlib.dumps(
            {"client_note_id": str(uuid.uuid4()), "text": "hi", "asr": {"v": ascii_pad}}
        ).encode("utf-8"),
        headers={**headers, "content-type": "application/json"},
    )
    assert ok.status_code == 202, (ok.status_code, ok.text)
