"""compose.yaml の build: があるサービスをビルドし、registry.noodles.jp に push する。

イメージの名前は registry.noodles.jp/<owner>/<repo>/<サービス>:<コミット>（すべて小文字）。
置き場所がルートでなければ、サービス名の前に置き場所を付ける（Hub の modules/platform/server/compose.ts と同じ決まり）。

ビルドの層は GitHub Actions のキャッシュ（type=gha）に置き、次のビルドで使い回す。変わらなかった層は中身が同じになるので、
レジストリにすでにあるものとして push も飛ばされる。キャッシュへの書き出しは push と並べて流す。サービスが複数あるときは並べてビルドする。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import IO

import yaml

REGISTRY = "registry.noodles.jp"
# build.yml で入れる railpack と同じ版にする
RAILPACK_FRONTEND = "ghcr.io/railwayapp/railpack-frontend:v0.43.0"


def run(*args: str, log: IO[str] | None, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(args), file=log, flush=True)
    subprocess.run(args, check=True, env=None if env is None else {**os.environ, **env}, stdout=log, stderr=subprocess.STDOUT if log else None)


STEP = re.compile(r"^#(\d+) (.*)$")


def run_build(*args: str, log: IO[str] | None) -> None:
    """buildx を流し、最後に、手順ごとにキャッシュから出たか・何秒かかったかをまとめて出す。"""
    out = log or sys.stdout
    print("+", " ".join(args), file=out, flush=True)
    names: dict[str, str] = {}
    results: dict[str, str] = {}
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace") as proc:
        assert proc.stdout is not None
        for line in proc.stdout:
            out.write(line)
            m = STEP.match(line.rstrip())
            if not m:
                continue
            step, rest = m.groups()
            if rest == "CACHED" or re.fullmatch(r"DONE [0-9.]+s", rest):
                results[step] = "キャッシュ" if rest == "CACHED" else rest.removeprefix("DONE ")
            else:
                names.setdefault(step, rest)
    print("手順ごとの時間：", file=out)
    for step, result in results.items():
        print(f"  {result:>8}  {names.get(step, step)}", file=out)
    out.flush()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, args)


def image_segment(path: str, service: str) -> str:
    if path in ("", "."):
        return service
    p = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")
    return f"{p}-{service}"


def image_name(repo: str, path: str, service: str, commit: str) -> str:
    return f"{REGISTRY}/{repo.lower()}/{image_segment(path, service)}:{commit}"


def login(config_dir: Path) -> str:
    """GitHub ActionsのOIDCトークンを取り、crane が使う Docker の設定に置いて返す。トークンは数分で切れるので push の直前に呼ぶ。"""
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=" + REGISTRY
    req = urllib.request.Request(url, headers={"Authorization": "bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
    with urllib.request.urlopen(req, timeout=30) as res:
        token = json.load(res)["value"]
    print(f"::add-mask::{token}", flush=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.json"
    path.write_text(json.dumps({"auths": {REGISTRY: {"registrytoken": token}}}))
    path.chmod(0o600)
    return token


# レジストリの前の Cloudflare は 100MB を超える要求を断る（413）。crane は層を1回の要求で送るので、
# 大きい層だけ先に分けて送っておく（crane はレジストリにある層を飛ばす）
LARGE_BLOB = 90 * 1024 * 1024
BLOB_CHUNK = 64 * 1024 * 1024


def registry_request(method: str, url: str, token: str, data: bytes | None = None, headers: dict[str, str] | None = None):
    req = urllib.request.Request(url, data=data, method=method, headers={"Authorization": "Bearer " + token, **(headers or {})})
    try:
        return urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as e:
        if method == "HEAD" and e.code == 404:
            return None
        raise RuntimeError(f"{method} {url.split('?')[0]}: {e.code} {e.read()[:300]!r}") from e


def upload_large_blobs(layout: Path, ref: str, token: str, log: IO[str] | None) -> None:
    repo = ref.removeprefix(REGISTRY + "/").rsplit(":", 1)[0]
    base = f"https://{REGISTRY}/v2/{repo}/blobs/"
    for path in sorted((layout / "blobs" / "sha256").iterdir()):
        size = path.stat().st_size
        if size <= LARGE_BLOB:
            continue
        digest = "sha256:" + path.name
        if registry_request("HEAD", base + digest, token) is not None:
            continue
        print(f"大きい層を分けて送ります：{digest}（{size // (1024 * 1024)}MB）", file=log, flush=True)
        location = registry_request("POST", base + "uploads/", token, data=b"").headers["Location"]
        offset = 0
        with path.open("rb") as f:
            while chunk := f.read(BLOB_CHUNK):
                res = registry_request(
                    "PATCH",
                    urllib.parse.urljoin(base, location),
                    token,
                    data=chunk,
                    headers={"Content-Type": "application/octet-stream", "Content-Range": f"{offset}-{offset + len(chunk) - 1}"},
                )
                location = res.headers["Location"]
                offset += len(chunk)
        url = urllib.parse.urljoin(base, location)
        registry_request("PUT", url + ("&" if "?" in url else "?") + "digest=" + urllib.parse.quote(digest), token, data=b"")


def shortcut_plan(plan: dict) -> dict | None:
    """コマンドのない手順（前の手順の上にソースを置くだけ）を飛ばした Railpack の計画を返す。飛ばせなければ None。

    Python などでは build の手順が「install の結果の上にソースを置く」だけで、完成品はそこから依存とソースを取り出す。
    このままだと、ソースが変わるたびに install の結果（ビルド用のイメージと依存、数百MB）を取り寄せて展開することになる。
    完成品が、依存は install から、ソースは手元から直接取るようにすると、変わっていない層は取り寄せずに済む。
    """
    steps = {s.get("name"): s for s in plan.get("steps") or []}
    excluded = set(plan.get("exclude") or [])
    changed = False
    inputs: list[dict] = []
    for layer in (plan.get("deploy") or {}).get("inputs") or []:
        step = steps.get(layer.get("step"))
        if step is None or step.get("commands"):
            inputs.append(layer)
            continue
        below = step.get("inputs") or []
        if len(below) != 2 or set(below[0]) != {"step"} or below[1] != {"include": ["."], "local": True}:
            return None
        if any(i.get("step") == step["name"] for other in steps.values() for i in other.get("inputs") or []):
            return None
        include = layer.get("include") or []
        rest = {k: v for k, v in layer.items() if k != "step"}
        if include == ["."]:
            inputs += [{**rest, "step": below[0]["step"]}, {**rest, "local": True}]
        elif include and all(path.startswith("/") and not_from_source(path, excluded) for path in include):
            inputs.append({**rest, "step": below[0]["step"]})
        else:
            return None
        changed = True
    if not changed:
        return None
    return {**plan, "deploy": {**plan["deploy"], "inputs": inputs}}


def not_from_source(path: str, excluded: set[str]) -> bool:
    """絶対パスが、手元のソースからは来ない場所（/app の外か、ソースから除かれるディレクトリの中）なら True。"""
    if path != "/app" and not path.startswith("/app/"):
        return True
    parts = path.removeprefix("/app").strip("/").split("/")
    return parts[0] in excluded


def build(name: str, context: Path, dockerfile: Path | None, args: dict[str, str], layout: Path, scope: str, log: IO[str] | None) -> list[str] | None:
    """ビルドして layout に書き出す。キャッシュが使えるときは、キャッシュへ書き出すためのコマンドを返す。"""
    common = ["docker", "buildx", "build", "--provenance=false", "--sbom=false"]
    for k, v in args.items():
        common += ["--build-arg", f"{k}={v}"]
    fallback: list[str] | None = None
    if dockerfile is not None:
        common += ["-f", str(dockerfile), str(context)]
    else:
        plan = layout.parent / f"{name}-railpack-plan.json"
        run("railpack", "prepare", str(context), "--plan-out", str(plan), "--info-out", str(layout.parent / f"{name}-railpack-info.json"), log=log)
        railpack = ["--build-arg", f"BUILDKIT_SYNTAX={RAILPACK_FRONTEND}"]
        short = shortcut_plan(json.loads(plan.read_text()))
        if short is not None:
            short_plan = layout.parent / f"{name}-railpack-plan-short.json"
            short_plan.write_text(json.dumps(short))
            fallback = [*common, *railpack, "-f", str(plan), str(context)]
            common += [*railpack, "-f", str(short_plan), str(context)]
        else:
            common += [*railpack, "-f", str(plan), str(context)]
    output = ["--output", f"type=oci,tar=false,dest={layout}"]
    # キャッシュの読み書きには、build.yml が環境変数に出す Actions のトークンが要る
    cached = bool(os.environ.get("ACTIONS_RUNTIME_TOKEN"))
    read_cache = ["--cache-from", f"type=gha,scope={scope}"] if cached else []
    try:
        run_build(*common, *output, *read_cache, log=log)
    except subprocess.CalledProcessError:
        if fallback is None:
            raise
        print("::warning::手順を飛ばした計画でビルドできなかったので、Railpack の計画のままビルドし直します", file=log, flush=True)
        shutil.rmtree(layout, ignore_errors=True)
        common = fallback
        run_build(*common, *output, *read_cache, log=log)
    if not cached:
        return None
    return [*common, "--output", "type=cacheonly", "--cache-to", f"type=gha,scope={scope},mode=max,ignore-error=true"]


def build_and_push(name: str, spec: dict, base: Path, tmp: Path, log: IO[str] | None) -> None:
    app_path = str(os.environ.get("APP_PATH") or ".")
    context = (base / spec.get("context", ".")).resolve()
    df = context / (spec.get("dockerfile") or "Dockerfile")
    raw_args = spec.get("args") or {}
    if isinstance(raw_args, list):
        raw_args = dict(str(a).split("=", 1) for a in raw_args if "=" in str(a))
    args = {str(k): str(v) for k, v in raw_args.items() if v is not None}
    layout = tmp / name
    save_cache = build(name, context, df if df.exists() else None, args, layout, f"noodles-{image_segment(app_path, name)}", log)
    # キャッシュへの書き出しは時間がかかることがあるので、push と並べて流す。同じビルドをもう一度流すが、層はbuilderに残っているので作り直さない
    cache_log = open(tmp / f"{name}-cache.log", "w+")
    saving = subprocess.Popen(save_cache, stdout=cache_log, stderr=subprocess.STDOUT) if save_cache else None
    ref = image_name(os.environ["GITHUB_REPOSITORY"], app_path, name, os.environ["COMMIT"])
    # buildx の builder は ~/.docker にあるので、DOCKER_CONFIG を変えるのは crane だけにする
    docker_config = Path(os.environ["NOODLES_DOCKER_CONFIG"]) / name
    try:
        upload_large_blobs(layout, ref, login(docker_config), log)
        run("crane", "push", str(layout), ref, log=log, env={"DOCKER_CONFIG": str(docker_config)})
    finally:
        if saving is not None:
            code = saving.wait()
            if code != 0:
                cache_log.seek(0)
                print(cache_log.read(), file=log, flush=True)
                print("::warning::ビルドのキャッシュを書き出せませんでした（次のビルドが遅くなるだけで、今回のイメージには影響しません）", file=log, flush=True)
        cache_log.close()


def main() -> int:
    base = Path(os.environ.get("APP_PATH") or ".")
    compose = yaml.safe_load((base / "compose.yaml").read_text()) or {}
    targets: dict[str, dict] = {}
    for name, svc in (compose.get("services") or {}).items():
        spec = (svc or {}).get("build")
        if spec is not None:
            targets[name] = {"context": spec} if isinstance(spec, str) else spec
    failed = False
    with tempfile.TemporaryDirectory() as tmp:
        if len(targets) == 1:
            for name, spec in targets.items():
                build_and_push(name, spec, base, Path(tmp), None)
        elif targets:
            # 並べて流すと出力が混ざるので、サービスごとにファイルへ取っておき、終わった順ではなく名前の順にまとめて出す
            logs = {name: open(Path(tmp) / f"{name}.log", "w+") for name in targets}
            with ThreadPoolExecutor(max_workers=len(targets)) as pool:
                jobs = {name: pool.submit(build_and_push, name, spec, base, Path(tmp), logs[name]) for name, spec in targets.items()}
            for name, job in jobs.items():
                err = job.exception()
                print(f"::group::{name}" + ("（失敗）" if err else ""), flush=True)
                logs[name].seek(0)
                sys.stdout.write(logs[name].read())
                logs[name].close()
                print("::endgroup::", flush=True)
                if err:
                    print(f"::error::{name} のビルドに失敗しました：{err}", flush=True)
                    failed = True
    if failed:
        return 1
    print(f"{len(targets)}個のイメージを push しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
