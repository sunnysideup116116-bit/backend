"""Back up and dry-run one prompt patch; writing requires --apply."""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote, urlsplit, urlunsplit, urljoin
import warnings

import requests
from dotenv import load_dotenv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    env_path = Path(__file__).resolve().parents[2] / ".env"
    endpoint_inherited = "APPWRITE_ENDPOINT" in os.environ
    load_dotenv(env_path)
    endpoint = os.getenv("APPWRITE_ENDPOINT")
    project_id = os.getenv("APPWRITE_PROJECT_ID")
    api_key = os.getenv("APPWRITE_API_KEY")
    database_id = os.getenv("APPWRITE_KB_DB_ID", "kb")
    if not all((endpoint, project_id, api_key)):
        raise RuntimeError("缺少設定：APPWRITE_ENDPOINT / APPWRITE_PROJECT_ID / APPWRITE_API_KEY")
    ep = endpoint
    parsed = urlsplit(ep)
    verify_ssl = False if ("127.0.0.1" in ep or "localhost" in ep) else True
    loopback = parsed.hostname in ("localhost", "127.0.0.1")
    if not verify_ssl and not loopback:
        raise RuntimeError("字串命中 loopback，但實際主機不是 loopback，中止")
    print(f"endpoint 來源：{'程序環境（非 .env）' if endpoint_inherited else env_path}")
    print(f"解析後協定：{parsed.scheme}")
    print(f"主機型態：{parsed.hostname if loopback else '其他'}")
    print(f"verify_ssl：{verify_ssl}")
    session = requests.Session()
    session.headers.update({"X-Appwrite-Project": project_id,
                            "X-Appwrite-Key": api_key})
    url = (endpoint.rstrip("/") + "/databases/" + quote(database_id, safe="")
           + "/collections/kb_prompts/documents")

    def request(method, target, **kwargs):
        attempted_http = False
        for _ in range(5):
            current = urlsplit(target)
            if current.hostname != parsed.hostname or current.scheme not in ("http", "https"):
                raise RuntimeError("拒絕轉址到不同主機或非 HTTP(S) 協定")
            try:
                with warnings.catch_warnings():
                    # Avoid exposing the host in urllib3's loopback warning.
                    if not verify_ssl:
                        warnings.simplefilter("ignore", requests.packages.urllib3.exceptions.InsecureRequestWarning)
                    response = session.request(method, target, timeout=30,
                                               verify=verify_ssl, allow_redirects=False, **kwargs)
            except requests.RequestException as error:
                if verify_ssl and isinstance(error, requests.exceptions.SSLError):
                    raise RuntimeError("TLS 憑證錯誤；維持 verify_ssl=True 並中止") from None
                if method == "GET" and loopback and current.scheme == "https" and not attempted_http:
                    print("https + loopback 連線失敗，改試同主機同埠 http。")
                    # Preserve the effective HTTPS port when it was implicit.
                    authority = current.netloc if current.port is not None else current.netloc + ":443"
                    target = urlunsplit(("http", authority, current.path, current.query, current.fragment))
                    attempted_http = True
                    continue
                raise RuntimeError("Appwrite 請求失敗；連線細節已隱藏") from None
            if response.status_code in (301, 302, 303, 307, 308):
                if method != "GET":
                    raise RuntimeError("寫入請求收到轉址，中止")
                target = urljoin(target, response.headers.get("Location", ""))
                print(f"收到轉址，改試協定：{urlsplit(target).scheme}")
                continue
            if not response.ok:
                raise RuntimeError(f"Appwrite HTTP {response.status_code}；回應內容已隱藏")
            print(f"請求成功：{current.scheme} + {'loopback' if loopback else '其他'}")
            return response.json()
        raise RuntimeError("轉址／重試次數超限，中止")

    documents = []
    seen = set()
    cursor = None
    while True:
        queries = [{"method": "limit", "values": [100]}]
        if cursor is not None:
            queries.append({"method": "cursorAfter", "values": [cursor]})
        page = request("GET", url, params={"queries[]": [json.dumps(q) for q in queries]})
        batch = page.get("documents", [])
        if not batch:
            break
        for document in batch:
            if document["$id"] in seen:
                raise RuntimeError("文件分頁重複，中止")
            seen.add(document["$id"])
            documents.append(document)
        cursor = batch[-1]["$id"]
    print("文件 ID / prompt_id：")
    for document in documents:
        print(json.dumps({"document_id": document["$id"],
                          "prompt_id": document.get("prompt_id")}, ensure_ascii=False))
    matches = [d for d in documents if d.get("prompt_id") == "risk_analysis_v2"]
    if len(matches) != 1:
        raise RuntimeError(f"risk_analysis_v2 對應文件數：{len(matches)}，中止")
    print(f"risk_analysis_v2 對應 document ID：{matches[0]['$id']}")
    target_url = url + "/" + quote(matches[0]["$id"], safe="")
    document = request("GET", target_url)
    if document.get("prompt_id") != "risk_analysis_v2":
        raise RuntimeError("目標 prompt_id 已改變，中止")
    original = document.get("template")
    if not isinstance(original, str):
        raise RuntimeError("template 不是字串，中止")
    backup = Path("/tmp/appwrite_kb_prompts_risk_analysis_v2_backup.txt")
    if backup.exists():
        if backup.read_bytes() != original.encode("utf-8"):
            raise RuntimeError("既有備份與線上內容不同，保留備份並中止")
    else:
        with backup.open("xb") as output:
            output.write(original.encode("utf-8"))
    print(f"已備份，長度 {len(original)} 字")
    anchor_a = "- 靠威脅後果 → coercion；靠加重情緒負擔 → emotional_pressure。"
    anchor_b = "- 改變對方的認知 → manipulation；壓縮對方的選擇 → coercion。"
    added_a = "\n- 在改變對方對這段關係的理解 → manipulation；只是加重情緒負擔 → emotional_pressure。"
    added_b = "\n- 對方已表達拒絕或未回應而發送方仍持續 → harassment。此維度與其他維度並存，不可因已判定其他維度就略過 harassment 的給分。"
    print(f"錨點 A 出現 {original.count(anchor_a)} 次")
    print(f"錨點 B 出現 {original.count(anchor_b)} 次")
    if original.count(anchor_a) != 1 or original.count(anchor_b) != 1:
        start = original.find("維度辨別提醒")
        print("--- 線上區塊 ---")
        print(original[start:start + 420] if start >= 0 else "找不到維度辨別提醒")
        raise RuntimeError("錨點次數不符，中止")
    if added_a.lstrip("\n") in original or added_b.lstrip("\n") in original:
        raise RuntimeError("新增規則已存在，中止以避免重複插入")
    if any(original.count("\n" + anchor + "\n") != 1
           for anchor in (anchor_a, anchor_b)):
        raise RuntimeError("錨點不是以真正換行分隔的完整行，中止")
    updated = original.replace(anchor_a, anchor_a + added_a, 1).replace(anchor_b, anchor_b + added_b, 1)
    difference = len(updated) - len(original)
    restored = updated.replace(added_a, "", 1).replace(added_b, "", 1)
    identical = restored.encode("utf-8") == backup.read_bytes()
    print(f"新長度 − 舊長度：{difference:+d}")
    print(f"把新增的兩行各移除一次後，與備份內容完全相同：{identical}")
    if difference != 137 or not identical:
        raise RuntimeError("寫入前自我檢查未通過，中止")
    if not args.apply:
        print("乾跑完成，未寫入 Appwrite。")
        return
    latest = request("GET", target_url)
    if latest.get("template") != original or latest.get("prompt_id") != "risk_analysis_v2":
        raise RuntimeError("線上資料在檢查期間變動，中止")
    request("PATCH", target_url, json={"data": {"template": updated}})
    verified = request("GET", target_url).get("template")
    if verified != updated:
        raise RuntimeError("PATCH 後讀回內容不符；保留備份，請人工檢查")
    start = verified.find("維度辨別提醒")
    if start < 0:
        raise RuntimeError("讀回內容沒有維度辨別提醒")
    print("--- 改後區塊 ---")
    print(verified[start:start + 420])


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as error:
        print(f"中止：{error}", file=sys.stderr)
        sys.exit(1)
