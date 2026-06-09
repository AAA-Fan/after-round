#!/usr/bin/env python3
"""Archive after_round Markdown outputs to Feishu docs and Bitable.

This script intentionally avoids printing app secrets or access tokens.
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.server
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any


CONFIG_PATH = Path("~/.codex/feishu_config.json").expanduser()
TOKEN_CACHE_PATH = Path("~/.codex/feishu_token.json").expanduser()
REDIRECT_URI = "http://localhost:9998/callback"
OPEN_API = "https://open.feishu.cn/open-apis"
DEFAULT_SCOPES = [
    "docx:document",
    "bitable:app",
    "drive:drive",
]
OUTPUT_SUBDIRS = {
    "status": "飞书归档状态",
    "result": "飞书归档结果",
}
ROUND_SUFFIXES = [
    "一面",
    "二面",
    "三面",
    "四面",
    "五面",
    "六面",
    "HR面",
    "HR 面",
    "hr面",
    "hr 面",
    "模拟一面",
    "模拟二面",
]


class FeishuError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FeishuError(f"配置文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise FeishuError(f"配置文件不是合法 JSON：{path}") from exc


def write_private_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def validate_config(cfg: dict[str, Any]) -> tuple[str, str]:
    app_id = str(cfg.get("app_id", "")).strip()
    app_secret = str(cfg.get("app_secret", "")).strip()
    placeholders = {"你的 App ID", "你的 App Secret", ""}
    if app_id in placeholders or app_secret in placeholders:
        raise FeishuError(f"{CONFIG_PATH} 中 app_id/app_secret 未配置完整")
    return app_id, app_secret


def parse_json_response(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        return {}
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text)
        return obj
    except json.JSONDecodeError as exc:
        sample = text[:500].replace("\n", "\\n")
        raise FeishuError(f"飞书 API 返回不是 JSON：{sample}") from exc


def curl_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    data: dict[str, Any] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    cmd = ["curl", "-sk", "-X", method.upper(), url, "--noproxy", "*"]
    headers = ["Content-Type: application/json"]
    if token:
        headers.append(f"Authorization: Bearer {token}")
    for header in headers:
        cmd.extend(["-H", header])

    temp_name: str | None = None
    if data is not None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump(data, tmp, ensure_ascii=False)
            temp_name = tmp.name
        cmd.extend(["--data", f"@{temp_name}"])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass

    if result.returncode != 0:
        raise FeishuError(f"curl 调用失败：{result.stderr.strip() or result.returncode}")
    return parse_json_response(result.stdout)


def curl_no_body(method: str, url: str, *, token: str, timeout: int = 30) -> dict[str, Any]:
    result = subprocess.run(
        [
            "curl",
            "-sk",
            "-X",
            method.upper(),
            url,
            "--noproxy",
            "*",
            "-H",
            f"Authorization: Bearer {token}",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise FeishuError(f"curl 调用失败：{result.stderr.strip() or result.returncode}")
    return parse_json_response(result.stdout)


def first_nonempty(*values: str | None) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def output_stem(args: argparse.Namespace) -> str:
    return f"{args.company}-{args.position}-{args.round_name}"


def default_outputs_root() -> Path:
    return Path.cwd() / "outputs"


def default_result_json_path(args: argparse.Namespace) -> Path:
    return default_outputs_root() / OUTPUT_SUBDIRS["result"] / f"{output_stem(args)}-飞书归档结果.json"


def default_status_md_path(args: argparse.Namespace) -> Path:
    return default_outputs_root() / OUTPUT_SUBDIRS["status"] / f"{output_stem(args)}-飞书归档状态.md"


def write_archive_artifacts(
    result: dict[str, Any],
    *,
    result_json: Path | None,
    status_md: Path | None,
    token_mode: str,
    note: str = "",
) -> None:
    if result_json:
        result_json.parent.mkdir(parents=True, exist_ok=True)
        result_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not status_md:
        return

    docs = result.get("docs", {})
    folders = result.get("folders", {})
    bitable_error = result.get("bitable_error")
    lines = [
        f"# {result.get('company', '')} {result.get('position', '')} {result.get('round', '')} - 飞书归档状态",
        "",
        f"归档时间：{dt.date.today().isoformat()}",
        "",
        "## 状态",
        "",
        "- 完整对话：已归档，可通过下方链接访问",
        "- 问答整理：已归档，可通过下方链接访问",
        "- 面试总评：已归档，可通过下方链接访问",
        f"- 多维表格：{'归档失败' if bitable_error else '已新增记录'}",
        f"- token 模式：{token_mode} token",
    ]
    if bitable_error:
        lines.append(f"- 多维表格错误：{bitable_error}")
    if note:
        lines.append(f"- 备注：{note}")
    lines.extend(
        [
            "",
            "## 飞书链接",
            "",
            f"- 完整对话：{docs.get('full', '')}",
            f"- 问答整理：{docs.get('qa', '')}",
            f"- 面试总评：{docs.get('summary', '')}",
            f"- 多维表格：{result.get('bitable') or ''}",
            "",
            "## 文件夹",
            "",
            f"- After round：{folders.get('after_round', '')}",
            f"- 完整对话：{folders.get('full', '')}",
            f"- 问答整理：{folders.get('qa', '')}",
            f"- 面试总评：{folders.get('summary', '')}",
            "",
        ]
    )
    status_md.parent.mkdir(parents=True, exist_ok=True)
    status_md.write_text("\n".join(lines), encoding="utf-8")


def get_tenant_token(app_id: str, app_secret: str) -> str:
    resp = curl_json(
        "POST",
        f"{OPEN_API}/auth/v3/tenant_access_token/internal",
        data={"app_id": app_id, "app_secret": app_secret},
        timeout=30,
    )
    token = resp.get("tenant_access_token")
    if not token:
        raise FeishuError(f"获取 tenant_access_token 失败：code={resp.get('code')} msg={resp.get('msg')}")
    return token


def load_cached_user_token() -> str | None:
    if not TOKEN_CACHE_PATH.exists():
        return None
    try:
        data = read_json(TOKEN_CACHE_PATH)
    except FeishuError:
        return None
    if time.time() < float(data.get("expires_at", 0)):
        token = data.get("access_token")
        return str(token) if token else None
    return None


def free_oauth_port() -> None:
    try:
        result = subprocess.run(["lsof", "-ti:9998"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return
    for pid in result.stdout.splitlines():
        pid = pid.strip()
        if pid:
            subprocess.run(["kill", "-9", pid], capture_output=True, text=True, timeout=5)


def open_auth_url(auth_url: str) -> None:
    opened = False
    try:
        opened = bool(webbrowser.open_new(auth_url))
    except Exception:
        opened = False
    if not opened and platform.system() == "Darwin":
        try:
            subprocess.run(["open", auth_url], capture_output=True, text=True, timeout=10)
            opened = True
        except Exception:
            opened = False
    if not opened:
        print("无法自动打开浏览器，请手动打开以下授权链接：", flush=True)
        print(auth_url, flush=True)


def build_oauth_authorize_url(app_id: str) -> str:
    return (
        f"{OPEN_API}/authen/v1/authorize"
        f"?app_id={urllib.parse.quote(app_id)}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
        f"&scope={urllib.parse.quote(' '.join(DEFAULT_SCOPES), safe='')}"
        f"&response_type=code"
    )


def extract_oauth_code(value: str) -> str:
    text = value.strip()
    if not text:
        raise FeishuError("OAuth code 为空")
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme and parsed.netloc:
        params = urllib.parse.parse_qs(parsed.query)
        if params.get("error"):
            detail = params.get("error_description", [""])[0]
            raise FeishuError(f"OAuth 授权失败：{params['error'][0]} {detail}".strip())
        code = params.get("code", [""])[0].strip()
        if not code:
            raise FeishuError("OAuth 回调 URL 中没有 code 参数")
        return code
    return text


def exchange_oauth_code(
    app_id: str,
    app_secret: str,
    code: str,
    *,
    tenant_token: str | None = None,
) -> str:
    token = tenant_token or get_tenant_token(app_id, app_secret)
    resp = curl_json(
        "POST",
        f"{OPEN_API}/authen/v1/access_token",
        token=token,
        data={"grant_type": "authorization_code", "code": code},
        timeout=30,
    )
    data = resp.get("data", {})
    access_token = data.get("access_token")
    if resp.get("code") != 0 or not access_token:
        raise FeishuError(f"获取 user_access_token 失败：code={resp.get('code')} msg={resp.get('msg')} data_keys={list(data)}")

    token_data = {
        "access_token": access_token,
        "refresh_token": data.get("refresh_token", ""),
        "expires_at": int(time.time()) + int(data.get("expires_in", 7200)) - 300,
    }
    write_private_json(TOKEN_CACHE_PATH, token_data)
    return str(access_token)


def oauth_flow(app_id: str, app_secret: str, *, no_browser: bool = False) -> str:
    tenant_token = get_tenant_token(app_id, app_secret)
    free_oauth_port()
    code_holder: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if params.get("code"):
                code_holder["code"] = params["code"][0]
            if params.get("error"):
                code_holder["error"] = params["error"][0]
                code_holder["error_description"] = params.get("error_description", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("授权已收到，可以关闭这个页面。".encode("utf-8"))

        def log_message(self, *_args: Any) -> None:
            return

    server = http.server.HTTPServer(("localhost", 9998), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    auth_url = build_oauth_authorize_url(app_id)
    print("飞书 OAuth 授权链接：", flush=True)
    print(f"授权链接：{auth_url}", flush=True)
    if no_browser:
        print("已启用 --no-browser：请复制上面的授权链接到浏览器打开。", flush=True)
    else:
        print("正在打开飞书 OAuth 授权页面，请在浏览器中完成授权...", flush=True)
        open_auth_url(auth_url)
    thread.join(timeout=180)
    server.server_close()

    if code_holder.get("error"):
        raise FeishuError(
            "OAuth 授权失败："
            f"{code_holder.get('error')} {code_holder.get('error_description', '')}".strip()
        )

    code = code_holder.get("code")
    if not code:
        raise FeishuError(
            "180 秒内未收到 OAuth 授权码。"
            "如果浏览器没有成功回调 localhost，请复制浏览器地址栏中的完整回调 URL 后重跑："
            "--oauth-callback-url 'http://localhost:9998/callback?code=...'；"
            "或只复制 code 后重跑：--oauth-code '...'"
        )

    return exchange_oauth_code(app_id, app_secret, code, tenant_token=tenant_token)


def get_user_token(
    app_id: str,
    app_secret: str,
    *,
    no_browser: bool = False,
    oauth_callback_url: str = "",
    oauth_code: str = "",
) -> str:
    manual_code = oauth_code.strip()
    if oauth_callback_url.strip():
        manual_code = extract_oauth_code(oauth_callback_url)
    if manual_code:
        print("使用手动提供的 OAuth code 换取 Feishu user token。", flush=True)
        return exchange_oauth_code(app_id, app_secret, manual_code)

    cached = load_cached_user_token()
    if cached:
        print("检测到可用的 Feishu user token 缓存，直接复用。", flush=True)
        return cached
    return oauth_flow(app_id, app_secret, no_browser=no_browser)


def text_elements(text: str, *, force_bold: bool = False) -> list[dict[str, Any]]:
    if force_bold:
        return [{"text_run": {"content": text, "text_element_style": {"bold": True}}}]

    parts = re.split(r"(\*\*[^*]+\*\*)", text)
    elements: list[dict[str, Any]] = []
    for part in parts:
        if not part:
            continue
        bold = part.startswith("**") and part.endswith("**")
        content = part[2:-2] if bold else part
        if not content:
            continue
        run: dict[str, Any] = {"content": content}
        if bold:
            run["text_element_style"] = {"bold": True}
        elements.append({"text_run": run})
    return elements or [{"text_run": {"content": " "}}]


def paragraph(text: str, *, bold: bool = False) -> dict[str, Any]:
    return {"block_type": 2, "text": {"elements": text_elements(text, force_bold=bold), "style": {}}}


def heading(text: str, level: int) -> dict[str, Any]:
    block_type = 3 if level <= 1 else 4 if level == 2 else 5 if level == 3 else 6
    key = "heading1" if block_type == 3 else "heading2" if block_type == 4 else "heading3" if block_type == 5 else "heading4"
    return {"block_type": block_type, key: {"elements": text_elements(text, force_bold=True), "style": {}}}


def divider() -> dict[str, Any]:
    return {"block_type": 22, "divider": {}}


def split_long_line(line: str, max_len: int = 1800) -> list[str]:
    if len(line) <= max_len:
        return [line]
    return [line[i : i + max_len] for i in range(0, len(line), max_len)]


def markdown_to_blocks(markdown: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    in_code_fence = False
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if set(stripped) <= {"-", "—", "─", "_"} and len(stripped) >= 3:
            blocks.append(divider())
            continue
        if not in_code_fence:
            match = re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
            if match:
                blocks.append(heading(match.group(2), min(len(match.group(1)), 4)))
                continue
        for part in split_long_line(line):
            blocks.append(paragraph(part))
    return blocks


def with_info_header(content: str, args: argparse.Namespace) -> str:
    header = [
        f"公司：{args.company}",
        f"岗位：{args.position}",
        f"轮次：{args.round_name}",
        f"日期：{args.date}",
    ]
    if args.duration:
        header.append(f"面试时长：{args.duration}")
    if args.interviewer:
        header.append(f"面试官：{args.interviewer}")
    return "\n".join(header) + "\n────────────────────────\n\n" + content


def create_doc(token: str, title: str) -> str:
    resp = curl_json("POST", f"{OPEN_API}/docx/v1/documents", token=token, data={"title": title}, timeout=60)
    if resp.get("code") != 0:
        raise FeishuError(f"创建文档失败：title={title} code={resp.get('code')} msg={resp.get('msg')}")
    try:
        return str(resp["data"]["document"]["document_id"])
    except KeyError as exc:
        raise FeishuError(f"创建文档返回缺少 document_id：{resp}") from exc


def write_blocks(token: str, doc_id: str, blocks: list[dict[str, Any]]) -> None:
    url = f"{OPEN_API}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children"
    for start in range(0, len(blocks), 20):
        batch = blocks[start : start + 20]
        last_resp: dict[str, Any] | None = None
        for attempt in range(1, 4):
            resp = curl_json("POST", url, token=token, data={"children": batch, "index": -1}, timeout=90)
            last_resp = resp
            if resp.get("code") == 0:
                break
            print(
                f"  写入 doc={doc_id} batch={start // 20 + 1} attempt={attempt} "
                f"失败：code={resp.get('code')} msg={resp.get('msg')}",
                flush=True,
            )
            time.sleep(1.5 * attempt)
        else:
            raise FeishuError(f"写入文档失败：doc={doc_id} resp={last_resp}")
        time.sleep(0.35)


def doc_link(doc_id: str) -> str:
    return f"https://feishu.cn/docx/{doc_id}"


def move_drive_file(token: str, file_token: str, file_type: str, folder_token: str) -> None:
    if not folder_token:
        return
    resp = curl_json(
        "POST",
        f"{OPEN_API}/drive/v1/files/{file_token}/move",
        token=token,
        data={"type": file_type, "folder_token": folder_token},
        timeout=60,
    )
    if resp.get("code") != 0:
        raise FeishuError(
            f"移动 {file_type} 到目标文件夹失败：file={file_token} "
            f"folder={folder_token} code={resp.get('code')} msg={resp.get('msg')}"
        )


def create_doc_from_markdown(token: str, title: str, markdown: str, folder_token: str = "") -> str:
    doc_id = create_doc(token, title)
    blocks = markdown_to_blocks(markdown)
    if blocks:
        write_blocks(token, doc_id, blocks)
    if folder_token:
        move_drive_file(token, doc_id, "docx", folder_token)
        print(f"已移动文档到目标文件夹：{title}", flush=True)
    print(f"已创建文档：{title} -> {doc_link(doc_id)}", flush=True)
    return doc_id


def list_records(token: str, base_url: str) -> list[dict[str, Any]]:
    resp = curl_no_body("GET", f"{base_url}/records?page_size=100", token=token, timeout=30)
    if resp.get("code") != 0:
        raise FeishuError(f"读取多维表格记录失败：code={resp.get('code')} msg={resp.get('msg')}")
    return list(resp.get("data", {}).get("items", []))


def list_fields(token: str, fields_url: str) -> list[dict[str, Any]]:
    resp = curl_no_body("GET", f"{fields_url}?page_size=100", token=token, timeout=30)
    if resp.get("code") != 0:
        raise FeishuError(f"读取多维表格字段失败：code={resp.get('code')} msg={resp.get('msg')}")
    return list(resp.get("data", {}).get("items", []))


def create_bitable_in_folder(token: str, folder_token: str) -> str:
    resp = curl_json(
        "POST",
        f"{OPEN_API}/drive/explorer/v2/file/{folder_token}",
        token=token,
        data={"title": "面试记录", "type": "bitable"},
        timeout=60,
    )
    if resp.get("code") != 0:
        raise FeishuError(f"在指定文件夹创建多维表格失败：code={resp.get('code')} msg={resp.get('msg')}")
    app_token = str(resp.get("data", {}).get("token", ""))
    if not app_token:
        raise FeishuError(f"在指定文件夹创建多维表格返回缺少 token：{resp}")
    return app_token


def create_bitable_if_needed(token: str, args: argparse.Namespace) -> tuple[str | None, str | None]:
    cfg = read_json(CONFIG_PATH)
    app_token = str(cfg.get("interview_bitable_app_token", "")).strip()
    table_id = str(cfg.get("interview_bitable_table_id", "")).strip()
    bitable_folder_token = first_nonempty(
        args.after_round_folder_token,
        cfg.get("after_round_folder_token"),
        cfg.get("interview_bitable_folder_token"),
    )
    if app_token and table_id:
        if bitable_folder_token:
            try:
                move_drive_file(token, app_token, "bitable", bitable_folder_token)
            except FeishuError as exc:
                print(f"移动已有多维表格失败，继续写入记录：{exc}", flush=True)
        return app_token, table_id

    print("未检测到多维表格配置，正在自动创建「面试记录」...", flush=True)
    if bitable_folder_token:
        app_token = create_bitable_in_folder(token, bitable_folder_token)
        print("多维表格已创建在 After round 文件夹下。", flush=True)
    else:
        resp = curl_json("POST", f"{OPEN_API}/bitable/v1/apps", token=token, data={"name": "面试记录"}, timeout=60)
        if resp.get("code") != 0:
            raise FeishuError(f"创建多维表格失败：code={resp.get('code')} msg={resp.get('msg')}")

        app_token = str(resp.get("data", {}).get("app", {}).get("app_token", ""))
        if not app_token:
            raise FeishuError("创建多维表格返回缺少 app_token")

    tables_resp = curl_no_body("GET", f"{OPEN_API}/bitable/v1/apps/{app_token}/tables", token=token, timeout=30)
    items = tables_resp.get("data", {}).get("items", [])
    if not items:
        raise FeishuError("多维表格没有默认表")
    table_id = str(items[0]["table_id"])

    fields_url = f"{OPEN_API}/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    records_url = f"{OPEN_API}/bitable/v1/apps/{app_token}/tables/{table_id}/records"

    try:
        records_resp = curl_no_body("GET", f"{records_url}?page_size=100", token=token, timeout=30)
        for record in records_resp.get("data", {}).get("items", []):
            if not record.get("fields"):
                curl_no_body("DELETE", f"{records_url}/{record['record_id']}", token=token, timeout=15)
    except FeishuError as exc:
        print(f"清理默认空记录失败，继续执行：{exc}", flush=True)

    try:
        fields = list_fields(token, fields_url)
        for field in fields:
            if field.get("is_primary"):
                curl_json("PUT", f"{fields_url}/{field['field_id']}", token=token, data={"field_name": "公司", "type": 1}, timeout=30)
                break
        for field in fields:
            if not field.get("is_primary"):
                curl_no_body("DELETE", f"{fields_url}/{field['field_id']}", token=token, timeout=15)
    except FeishuError as exc:
        print(f"调整默认字段失败，继续执行：{exc}", flush=True)

    for field in [
        {"field_name": "岗位", "type": 1},
        {"field_name": "面试轮次", "type": 1},
        {"field_name": "面试时间", "type": 5},
        {"field_name": "面试时长", "type": 1},
        {"field_name": "面试官", "type": 1},
        {"field_name": "面试整理版", "type": 15},
        {"field_name": "面试问答整理", "type": 15},
        {"field_name": "面试总评", "type": 15},
        {"field_name": "备注", "type": 1},
    ]:
        resp = curl_json("POST", fields_url, token=token, data=field, timeout=30)
        if resp.get("code") != 0:
            print(f"创建字段 {field['field_name']} 失败，继续执行：code={resp.get('code')} msg={resp.get('msg')}", flush=True)

    cfg["interview_bitable_app_token"] = app_token
    cfg["interview_bitable_table_id"] = table_id
    write_private_json(CONFIG_PATH, cfg)
    print(f"多维表格已创建：https://feishu.cn/base/{app_token}", flush=True)
    return app_token, table_id


def update_bitable(
    token: str,
    args: argparse.Namespace,
    doc_ids: dict[str, str],
) -> str | None:
    created = create_bitable_if_needed(token, args)
    app_token, table_id = created
    if not app_token or not table_id:
        return None

    base_url = f"{OPEN_API}/bitable/v1/apps/{app_token}/tables/{table_id}"
    company_label = args.company
    record_label = f"{args.company}{args.round_name}"

    year, month, day = map(int, args.date.split("-"))
    timestamp_ms = int(dt.datetime(year, month, day).timestamp() * 1000)
    fields: dict[str, Any] = {
        "公司": company_label,
        "岗位": args.position,
        "面试轮次": args.round_name,
        "面试时间": timestamp_ms,
        "面试整理版": {"text": f"{record_label}-完整对话", "link": doc_link(doc_ids["full"])},
        "面试问答整理": {"text": f"{record_label}-问答整理", "link": doc_link(doc_ids["qa"])},
        "面试总评": {"text": f"{record_label}-面试总评", "link": doc_link(doc_ids["summary"])},
    }
    if args.interviewer:
        fields["面试官"] = args.interviewer
    if args.duration:
        fields["面试时长"] = args.duration

    resp = curl_json("POST", f"{base_url}/records", token=token, data={"fields": fields}, timeout=30)
    if resp.get("code") != 0:
        raise FeishuError(f"写入多维表格记录失败：code={resp.get('code')} msg={resp.get('msg')} resp={resp}")

    print("多维表格记录已新建。", flush=True)
    return f"https://feishu.cn/base/{app_token}?table={table_id}"


def split_company_round_suffix(company: str) -> tuple[str, str | None]:
    value = company.strip()
    for suffix in sorted(ROUND_SUFFIXES, key=len, reverse=True):
        pattern = rf"^(?P<company>.+?)\s*{re.escape(suffix)}$"
        match = re.match(pattern, value)
        if match:
            pure_company = match.group("company").strip()
            if pure_company:
                normalized_suffix = suffix.replace(" ", "")
                if normalized_suffix.lower().startswith("hr"):
                    normalized_suffix = "HR面"
                return pure_company, normalized_suffix
    return value, None


def repair_bitable_company_names(token: str, app_token: str, table_id: str) -> int:
    base_url = f"{OPEN_API}/bitable/v1/apps/{app_token}/tables/{table_id}"
    records = list_records(token, base_url)
    repaired = 0
    for record in records:
        record_id = record.get("record_id")
        fields = record.get("fields", {})
        company = str(fields.get("公司", "")).strip()
        if not record_id or not company:
            continue
        pure_company, inferred_round = split_company_round_suffix(company)
        if pure_company == company:
            continue
        update_fields: dict[str, Any] = {"公司": pure_company}
        if inferred_round and not str(fields.get("面试轮次", "")).strip():
            update_fields["面试轮次"] = inferred_round
        resp = curl_json(
            "PUT",
            f"{base_url}/records/{record_id}",
            token=token,
            data={"fields": update_fields},
            timeout=30,
        )
        if resp.get("code") != 0:
            raise FeishuError(
                f"修复多维表格公司名失败：record_id={record_id} code={resp.get('code')} msg={resp.get('msg')}"
            )
        repaired += 1
        round_note = f"，面试轮次={update_fields['面试轮次']}" if "面试轮次" in update_fields else ""
        print(f"已修复公司名：{company} -> {pure_company}{round_note}", flush=True)
    return repaired


def move_existing_assets(
    token: str,
    cfg: dict[str, Any],
    args: argparse.Namespace,
    doc_ids: dict[str, str],
    folder_tokens: dict[str, str],
) -> str | None:
    moved: list[str] = []
    if folder_tokens["full"]:
        move_drive_file(token, doc_ids["full"], "docx", folder_tokens["full"])
        moved.append("完整对话")
    if folder_tokens["qa"]:
        move_drive_file(token, doc_ids["qa"], "docx", folder_tokens["qa"])
        moved.append("问答整理")
    if folder_tokens["summary"]:
        move_drive_file(token, doc_ids["summary"], "docx", folder_tokens["summary"])
        moved.append("面试总评")

    bitable_link = None
    app_token = str(cfg.get("interview_bitable_app_token", "")).strip()
    table_id = str(cfg.get("interview_bitable_table_id", "")).strip()
    bitable_folder_token = first_nonempty(args.after_round_folder_token, cfg.get("after_round_folder_token"))
    if app_token and bitable_folder_token:
        move_drive_file(token, app_token, "bitable", bitable_folder_token)
        moved.append("多维表格")
    if app_token and table_id:
        bitable_link = f"https://feishu.cn/base/{app_token}?table={table_id}"

    print("已移动资产：" + ("、".join(moved) if moved else "无"), flush=True)
    return bitable_link


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", required=True)
    parser.add_argument("--position", required=True)
    parser.add_argument("--round-name", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--duration", default="")
    parser.add_argument("--interviewer", default="")
    parser.add_argument("--full", type=Path)
    parser.add_argument("--qa", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--existing-full-doc-id", default="")
    parser.add_argument("--existing-qa-doc-id", default="")
    parser.add_argument("--existing-summary-doc-id", default="")
    parser.add_argument("--after-round-folder-token", default="")
    parser.add_argument("--full-dialogue-folder-token", default="")
    parser.add_argument("--qa-folder-token", default="")
    parser.add_argument("--summary-folder-token", default="")
    parser.add_argument(
        "--move-existing-only",
        action="store_true",
        help="Only move existing docx/bitable assets to configured folders. Do not create docs or add Bitable records.",
    )
    parser.add_argument("--result-json", type=Path)
    parser.add_argument(
        "--status-md",
        type=Path,
        help="Write a local Feishu archive status Markdown file. Defaults to outputs/飞书归档状态/.",
    )
    parser.add_argument(
        "--token-mode",
        choices=["user", "tenant"],
        default="user",
        help="Use OAuth user token by default. Tenant mode skips OAuth and uses the app tenant token.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="User token 模式下只打印 OAuth 授权链接，不尝试自动打开浏览器。",
    )
    parser.add_argument(
        "--oauth-callback-url",
        default="",
        help="User token 模式下手动粘贴浏览器回调完整 URL，例如 http://localhost:9998/callback?code=...",
    )
    parser.add_argument(
        "--oauth-code",
        default="",
        help="User token 模式下手动粘贴 OAuth code，适合 localhost 回调失败后重跑。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.result_json is None:
        args.result_json = default_result_json_path(args)
    if args.status_md is None:
        args.status_md = default_status_md_path(args)
    existing_doc_ids = {
        "full": args.existing_full_doc_id.strip(),
        "qa": args.existing_qa_doc_id.strip(),
        "summary": args.existing_summary_doc_id.strip(),
    }
    use_existing_docs = all(existing_doc_ids.values())
    if args.move_existing_only and not use_existing_docs:
        raise FeishuError("--move-existing-only 必须同时提供三个 --existing-*-doc-id")
    if not use_existing_docs:
        if not args.full or not args.qa or not args.summary:
            raise FeishuError("未提供已有 doc ID 时，必须提供 --full/--qa/--summary 三个 Markdown 文件")
        for path in [args.full, args.qa, args.summary]:
            if not path.exists():
                raise FeishuError(f"找不到待归档文件：{path}")

    cfg = read_json(CONFIG_PATH)
    app_id, app_secret = validate_config(cfg)
    folder_tokens = {
        "full": first_nonempty(
            args.full_dialogue_folder_token,
            cfg.get("after_round_full_dialogue_folder_token"),
            cfg.get("full_dialogue_folder_token"),
        ),
        "qa": first_nonempty(
            args.qa_folder_token,
            cfg.get("after_round_qa_folder_token"),
            cfg.get("qa_folder_token"),
        ),
        "summary": first_nonempty(
            args.summary_folder_token,
            cfg.get("after_round_summary_folder_token"),
            cfg.get("summary_folder_token"),
        ),
    }
    if args.token_mode == "tenant":
        if args.oauth_callback_url or args.oauth_code:
            raise FeishuError("--oauth-callback-url / --oauth-code 只适用于 --token-mode user")
        print("使用 tenant token 模式，跳过 OAuth。", flush=True)
        token = get_tenant_token(app_id, app_secret)
    else:
        token = get_user_token(
            app_id,
            app_secret,
            no_browser=args.no_browser,
            oauth_callback_url=args.oauth_callback_url,
            oauth_code=args.oauth_code,
        )

    if args.move_existing_only:
        print("只移动已有飞书资产，不创建文档、不新增多维表格记录。", flush=True)
        bitable_link = move_existing_assets(token, cfg, args, existing_doc_ids, folder_tokens)
        result = {
            "company": args.company,
            "position": args.position,
            "round": args.round_name,
            "date": args.date,
            "docs": {
                "full": doc_link(existing_doc_ids["full"]),
                "qa": doc_link(existing_doc_ids["qa"]),
                "summary": doc_link(existing_doc_ids["summary"]),
            },
            "bitable": bitable_link,
            "bitable_error": None,
            "folders": {
                "after_round": first_nonempty(args.after_round_folder_token, cfg.get("after_round_folder_token")),
                "full": folder_tokens["full"],
                "qa": folder_tokens["qa"],
                "summary": folder_tokens["summary"],
            },
        }
        write_archive_artifacts(
            result,
            result_json=args.result_json,
            status_md=args.status_md,
            token_mode=args.token_mode,
            note="只移动已有飞书资产，不创建新文档。",
        )
        print("FEISHU_ARCHIVE_RESULT=" + json.dumps(result, ensure_ascii=False), flush=True)
        return 0

    if use_existing_docs:
        print("使用已有飞书文档 ID，只补写多维表格记录。", flush=True)
        doc_ids = existing_doc_ids
    else:
        full_md = with_info_header(args.full.read_text(encoding="utf-8"), args)
        qa_md = args.qa.read_text(encoding="utf-8")
        summary_md = args.summary.read_text(encoding="utf-8")

        title_prefix = f"{args.company} {args.position} {args.round_name}"
        doc_ids = {
            "full": create_doc_from_markdown(token, f"{title_prefix} - 完整对话", full_md, folder_tokens["full"]),
            "qa": create_doc_from_markdown(token, f"{title_prefix} - 问答整理", qa_md, folder_tokens["qa"]),
            "summary": create_doc_from_markdown(token, f"{title_prefix} - 面试总评", summary_md, folder_tokens["summary"]),
        }

    bitable_link: str | None = None
    bitable_error: str | None = None
    try:
        bitable_link = update_bitable(token, args, doc_ids)
    except FeishuError as exc:
        bitable_error = str(exc)
        print(f"多维表格归档失败，但三份文档已创建：{bitable_error}", flush=True)

    result = {
        "company": args.company,
        "position": args.position,
        "round": args.round_name,
        "date": args.date,
        "docs": {
            "full": doc_link(doc_ids["full"]),
            "qa": doc_link(doc_ids["qa"]),
            "summary": doc_link(doc_ids["summary"]),
        },
        "bitable": bitable_link,
        "bitable_error": bitable_error,
        "folders": {
            "after_round": first_nonempty(args.after_round_folder_token, cfg.get("after_round_folder_token")),
            "full": folder_tokens["full"],
            "qa": folder_tokens["qa"],
            "summary": folder_tokens["summary"],
        },
    }
    write_archive_artifacts(
        result,
        result_json=args.result_json,
        status_md=args.status_md,
        token_mode=args.token_mode,
    )

    print("FEISHU_ARCHIVE_RESULT=" + json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FeishuError as exc:
        print(f"错误：{exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
