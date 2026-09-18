import json

import httpx
import pytest

import scripts.query_knowledge as query_tool


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(query_tool, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("DIFY_API_KEY", raising=False)
    monkeypatch.delenv("KNOWLEDGE_ID", raising=False)
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    (tmp_path / ".env").write_text("DIFY_API_KEY=private-test-key\nKNOWLEDGE_ID=mud-ren-forum\n", encoding="utf-8")


def mock_client(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(query_tool.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


def record():
    return {"content": "LPC 示例正文", "score": 0.93, "title": "中文教程", "metadata": {"thread_id": 432, "url": "https://bbs.mud.ren/threads/432"}}


def test_single_query_sends_dify_protocol(configured, monkeypatch, capsys):
    def handler(request):
        assert str(request.url) == "http://127.0.0.1:8008/retrieval"
        assert request.headers["Authorization"] == "Bearer private-test-key"
        assert request.headers["Content-Type"] == "application/json"
        assert json.loads(request.content) == {
            "knowledge_id": "mud-ren-forum", "query": "LPC 如何通信？",
            "retrieval_setting": {"top_k": 3, "score_threshold": 0.0},
        }
        return httpx.Response(200, json={"records": [record()]})
    mock_client(monkeypatch, handler)
    assert query_tool.main(["--query", "LPC 如何通信？"]) == 0
    output = capsys.readouterr().out
    assert "中文教程" in output and "0.930000" in output and "HTTP 200" in output
    assert "private-test-key" not in output


def test_overrides_and_full_endpoint(configured, monkeypatch):
    monkeypatch.setenv("DIFY_API_KEY", "environment-key")
    def handler(request):
        assert str(request.url) == "https://rag.example/api/retrieval"
        assert request.headers["Authorization"] == "Bearer environment-key"
        assert json.loads(request.content)["knowledge_id"] == "override"
        assert json.loads(request.content)["retrieval_setting"] == {"top_k": 5, "score_threshold": 0.6}
        return httpx.Response(200, json={"records": []})
    mock_client(monkeypatch, handler)
    assert query_tool.main(["-q", "问题", "--url", "https://rag.example/api/retrieval/", "--knowledge-id", "override", "--top-k", "5", "--score", "0.6"]) == 0


def test_interactive_parameters(configured, monkeypatch, capsys):
    queries = iter(["", "/topk 0", "/topk 5", "/score 0.5", "中文查询", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(queries))
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"records": [record()]})
    mock_client(monkeypatch, handler)
    assert query_tool.main([]) == 0
    assert len(requests) == 1
    assert requests[0]["retrieval_setting"] == {"top_k": 5, "score_threshold": 0.5}
    assert "Top_K 必须" in capsys.readouterr().out


def test_rate_limit_without_retry_or_key_leak(configured, monkeypatch, capsys):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": "60"}, json={"error_code": 5006, "error_msg": "限流 private-test-key"})
    mock_client(monkeypatch, handler)
    assert query_tool.main(["-q", "问题"]) == 1
    output = capsys.readouterr().out
    assert len(calls) == 1 and "5006" in output and "Retry-After：60" in output
    assert "private-test-key" not in output


@pytest.mark.parametrize("body", [
    {"records": None}, {"records": [{"metadata": None}]},
    {"records": [{"content": "x", "title": "x", "score": 2, "metadata": {}}]},
])
def test_invalid_success_response(configured, monkeypatch, capsys, body):
    mock_client(monkeypatch, lambda _: httpx.Response(200, json=body))
    assert query_tool.main(["-q", "问题"]) == 1
    assert "响应格式错误" in capsys.readouterr().out


def test_connection_error(configured, monkeypatch, capsys):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)
    mock_client(monkeypatch, handler)
    assert query_tool.main(["-q", "问题"]) == 1
    assert "请确认服务已启动" in capsys.readouterr().out


@pytest.mark.parametrize("args", [
    ["--top-k", "0"], ["--top-k", "101"], ["--score", "nan"],
    ["--score", "1.1"], ["--timeout", "0"], ["--content-limit", "-1"],
    ["--url", "https://user:secret@example.com"], ["--url", "ftp://example.com"],
    ["--url", "http://localhost:bad"], ["--url", "http://localhost:65536"],
    ["--url", "http://localhost:0"], ["--url", "http://[broken"],
])
def test_invalid_arguments(args):
    with pytest.raises(SystemExit) as exc:
        query_tool.build_parser().parse_args(args)
    assert exc.value.code == 2


def test_missing_key(configured, monkeypatch, capsys):
    monkeypatch.setenv("DIFY_API_KEY", "")
    monkeypatch.setattr(query_tool.httpx, "Client", lambda **kwargs: pytest.fail("unexpected HTTP request"))
    assert query_tool.main(["-q", "问题"]) == 1
    assert "DIFY_API_KEY" in capsys.readouterr().out


def test_query_uses_env_host_and_port(configured, monkeypatch):
    (query_tool.PROJECT_ROOT / ".env").write_text(
        "DIFY_API_KEY=private-test-key\nKNOWLEDGE_ID=mud-ren-forum\nHOST=0.0.0.0\nPORT=9009\n",
        encoding="utf-8",
    )
    def handler(request):
        assert str(request.url) == "http://127.0.0.1:9009/retrieval"
        return httpx.Response(200, json={"records": []})
    mock_client(monkeypatch, handler)
    assert query_tool.main(["-q", "测试"]) == 0


def test_explicit_url_ignores_local_binding(configured, monkeypatch):
    monkeypatch.setenv("HOST", "invalid host")
    monkeypatch.setenv("PORT", "invalid port")
    def handler(request):
        assert str(request.url) == "https://rag.example/retrieval"
        return httpx.Response(200, json={"records": []})
    mock_client(monkeypatch, handler)
    assert query_tool.main(["-q", "测试", "--url", "https://rag.example"]) == 0


def test_bad_env_port_does_not_send_request(configured, monkeypatch, capsys):
    monkeypatch.setenv("PORT", "bad")
    monkeypatch.setattr(query_tool.httpx, "Client", lambda **kwargs: pytest.fail("unexpected HTTP request"))
    assert query_tool.main(["-q", "测试"]) == 1
    assert "PORT" in capsys.readouterr().out


def test_non_json_rate_limit_keeps_retry_hint(configured, monkeypatch, capsys):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "30"}, text="<html>Too many requests</html>")
    mock_client(monkeypatch, handler)
    assert query_tool.main(["-q", "测试"]) == 1
    output = capsys.readouterr().out
    assert len(calls) == 1
    assert "HTTP 429" in output and "Retry-After：30" in output
