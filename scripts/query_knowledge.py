"""模拟 Dify 调用外部知识库，交互查看中文召回结果。"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.server_config import ServerSettings

# 日常调试默认值；也可以使用命令行参数覆盖。
DEFAULT_TOP_K = 3
DEFAULT_SCORE_THRESHOLD = 0.0


def top_k_value(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Top_K 必须为 1～100 的整数。") from None
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("Top_K 必须为 1～100 的整数。")
    return number


def score_value(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Score 必须为 0～1 的数字。") from None
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise argparse.ArgumentTypeError("Score 必须为 0～1 的数字。")
    return number


def positive_seconds(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("超时秒数必须为正数。") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("超时秒数必须为正数。")
    return number


def content_limit_value(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("正文显示长度必须为非负整数，0 表示全文。") from None
    if number < 0:
        raise argparse.ArgumentTypeError("正文显示长度必须为非负整数，0 表示全文。")
    return number


def retrieval_url(value: str) -> str:
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise argparse.ArgumentTypeError("服务地址格式错误，请检查主机名、IPv6 地址及端口。") from None
    if (
        parsed.scheme not in ("http", "https") or not parsed.hostname
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or any(char.isspace() for char in value) or port is not None and not 1 <= port <= 65535
    ):
        raise argparse.ArgumentTypeError("服务地址应为 http(s) URL，端口为 1～65535，不能包含账号、密码、查询参数或片段。")
    return value if parsed.path.endswith("/retrieval") else value + "/retrieval"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-q", "--query", help="只查询一次；省略时进入交互模式")
    parser.add_argument("--url", type=retrieval_url, help="服务基础地址或完整 /retrieval 地址；默认由 .env 的 HOST、PORT 决定")
    parser.add_argument("--knowledge-id", help="覆盖 .env 中的 KNOWLEDGE_ID")
    parser.add_argument("--top-k", type=top_k_value, default=DEFAULT_TOP_K, help=f"最多返回条数，默认 {DEFAULT_TOP_K}")
    parser.add_argument("--score-threshold", "--score", type=score_value, default=DEFAULT_SCORE_THRESHOLD, help=f"最低得分，默认 {DEFAULT_SCORE_THRESHOLD}")
    parser.add_argument("--timeout", type=positive_seconds, default=120.0, help="网络阶段超时秒数，默认 120")
    parser.add_argument("--content-limit", type=content_limit_value, default=800, help="每条正文显示字符数，默认 800；0 为全文")
    return parser


def redact(text: str, api_key: str) -> str:
    return text.replace(api_key, "<已隐藏密钥>") if api_key else text


def query_once(client, args, query: str, api_key: str) -> int:
    payload = {
        "knowledge_id": args.knowledge_id,
        "query": query,
        "retrieval_setting": {"top_k": args.top_k, "score_threshold": args.score_threshold},
    }
    started = time.perf_counter()
    try:
        response = client.post(
            args.url, headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
    except httpx.HTTPError as exc:
        print(f"请求失败（{time.perf_counter() - started:.2f} 秒）：{redact(str(exc), api_key)}")
        print("请确认服务已启动、--url 地址可达；跨机器测试请使用服务器实际地址。")
        return 1
    print(f"HTTP {response.status_code} | 耗时 {time.perf_counter() - started:.2f} 秒 | Top_K={args.top_k} | Score≥{args.score_threshold:g}")
    retry_after = response.headers.get("Retry-After")
    if response.status_code != 200 and retry_after:
        print(f"Retry-After：{redact(retry_after, api_key)}；请等待后再试，脚本不会自动重复请求。")
    try:
        body = response.json()
    except ValueError:
        print("接口没有返回合法 JSON，请检查服务地址及代理配置。")
        return 1
    if response.status_code != 200:
        if isinstance(body, dict):
            print(redact(f"错误码：{body.get('error_code', '未知')}；原因：{body.get('error_msg', '请求失败')}", api_key))
        else:
            print("接口返回错误，请检查服务端日志。")
        return 1
    records = body.get("records") if isinstance(body, dict) else None
    if not isinstance(records, list):
        print("响应格式错误：缺少 records 数组。")
        return 1
    for record in records:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("content"), str)
            or not isinstance(record.get("title"), str)
            or not isinstance(record.get("metadata"), dict)
            or type(record.get("score")) not in (int, float)
            or not math.isfinite(record["score"])
            or not 0 <= record["score"] <= 1
        ):
            print("响应格式错误：结果需包含正文、标题、0～1 得分及对象形式的 metadata。")
            return 1
    print(f"召回 {len(records)} 条结果。")
    if not records:
        print("未找到符合条件的结果，可降低 Score 或换用已知帖子关键词。")
    for index, record in enumerate(records, 1):
        metadata = record["metadata"]
        content = record["content"]
        preview = content if args.content_limit == 0 else content[:args.content_limit]
        lines = [f"\n[{index}] {record['title']} | 得分 {record['score']:.6f}"]
        for field, label in (("thread_id", "帖子 ID"), ("url", "链接"), ("author", "作者"), ("published_at", "发布时间")):
            if metadata.get(field) is not None:
                lines.append(f"{label}：{metadata[field]}")
        lines.append(preview)
        if len(preview) < len(content):
            lines.append(f"……已省略 {len(content) - len(preview)} 字符；使用 --content-limit 0 显示全文。")
        print(redact("\n".join(lines), api_key))
    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    if args.url is None:
        try:
            args.url = retrieval_url(ServerSettings.from_env().local_url)
        except ValueError as exc:
            print(f"服务监听配置错误：{exc}")
            return 1
    api_key = os.getenv("DIFY_API_KEY", "").strip()
    if not api_key or api_key == "your_dify_api_key_here":
        print("请先在项目 .env 或进程环境变量中配置有效的 DIFY_API_KEY。")
        return 1
    args.knowledge_id = (args.knowledge_id if args.knowledge_id is not None else os.getenv("KNOWLEDGE_ID", "mud-ren-forum")).strip()
    if not args.knowledge_id:
        print("请配置 KNOWLEDGE_ID，或通过 --knowledge-id 指定 Dify 的外部知识库 ID。")
        return 1
    if args.query is not None and not args.query.strip():
        parser.error("查询内容不能为空。")
    # 直接连接指定服务，避免本机代理配置干扰 localhost 自测。
    with httpx.Client(timeout=args.timeout, trust_env=False, follow_redirects=False) as client:
        if args.query is not None:
            return query_once(client, args, args.query.strip(), api_key)
        print(f"服务：{args.url}\n知识库：{args.knowledge_id}\nTop_K={args.top_k}，Score≥{args.score_threshold:g}")
        print("输入问题后回车；/topk 5 和 /score 0.5 可调整参数；/exit 退出。")
        print("每次查询都会调用真实服务，请勿连续高频提交。")
        while True:
            try:
                query = input("\n请输入查询内容 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n已退出。")
                return 0
            if query.lower() in ("/exit", "/quit"):
                return 0
            if not query:
                continue
            if query.startswith("/"):
                parts = query.split()
                try:
                    if len(parts) == 2 and parts[0].lower() == "/topk":
                        args.top_k = top_k_value(parts[1])
                    elif len(parts) == 2 and parts[0].lower() == "/score":
                        args.score_threshold = score_value(parts[1])
                    else:
                        print("可用命令：/topk 5、/score 0.5、/exit。")
                        continue
                except argparse.ArgumentTypeError as exc:
                    print(str(exc))
                    continue
                print(f"已设置 Top_K={args.top_k}，Score≥{args.score_threshold:g}")
                continue
            query_once(client, args, query, api_key)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n查询已中断。")
        raise SystemExit(130)
