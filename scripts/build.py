"""compose.yaml の build: があるサービスをビルドし、registry.noodles.jp に push する。

イメージの名前は registry.noodles.jp/<owner>/<repo>/<サービス>:<コミット>（すべて小文字）。
置き場所がルートでなければ、サービス名の前に置き場所を付ける（Hub の modules/platform/server/compose.ts と同じ決まり）。
"""

import json
import os
import re
import shutil
import urllib.request
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import yaml

REGISTRY = "registry.noodles.jp"
# build.yml で入れる railpack と同じ版にする
RAILPACK_FRONTEND = "ghcr.io/railwayapp/railpack-frontend:v0.43.0"


def run(*args: str, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, check=True, env=None if env is None else {**os.environ, **env})


def image_name(repo: str, path: str, service: str, commit: str) -> str:
    seg = service
    if path not in ("", "."):
        p = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")
        seg = f"{p}-{service}"
    return f"{REGISTRY}/{repo.lower()}/{seg}:{commit}"


def login(config_dir: Path) -> None:
    """GitHub ActionsのOIDCトークンを取り、crane が使う Docker の設定に置く。トークンは数分で切れるので push の直前に呼ぶ。"""
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=" + REGISTRY
    req = urllib.request.Request(url, headers={"Authorization": "bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
    with urllib.request.urlopen(req, timeout=30) as res:
        token = json.load(res)["value"]
    print(f"::add-mask::{token}", flush=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.json"
    path.write_text(json.dumps({"auths": {REGISTRY: {"registrytoken": token}}}))
    path.chmod(0o600)


def build(name: str, context: Path, dockerfile: Path | None, args: dict[str, str], out: Path, cache: Path | None) -> None:
    common = ["docker", "buildx", "build", "--provenance=false", "--sbom=false", "--output", f"type=oci,dest={out}"]
    if cache is not None:
        if (cache / "index.json").exists():
            common += ["--cache-from", f"type=local,src={cache}"]
        common += ["--cache-to", f"type=local,dest={cache}.new,mode=max"]
    for k, v in args.items():
        common += ["--build-arg", f"{k}={v}"]
    if dockerfile is not None:
        run(*common, "-f", str(dockerfile), str(context))
        return
    plan = out.parent / f"{name}-railpack-plan.json"
    run("railpack", "prepare", str(context), "--plan-out", str(plan), "--info-out", str(out.parent / f"{name}-railpack-info.json"))
    run(*common, "--build-arg", f"BUILDKIT_SYNTAX={RAILPACK_FRONTEND}", "-f", str(plan), str(context))


def main() -> int:
    base = Path(os.environ.get("APP_PATH") or ".")
    repo = os.environ["GITHUB_REPOSITORY"]
    commit = os.environ["COMMIT"]
    compose = yaml.safe_load((base / "compose.yaml").read_text()) or {}
    services = compose.get("services") or {}
    built = 0
    # buildx の builder は ~/.docker にあるので、DOCKER_CONFIG を変えるのは crane だけにする
    docker_config = Path(os.environ["NOODLES_DOCKER_CONFIG"])
    with tempfile.TemporaryDirectory() as tmp:
        for name, svc in services.items():
            spec = (svc or {}).get("build")
            if spec is None:
                continue
            if isinstance(spec, str):
                spec = {"context": spec}
            context = (base / spec.get("context", ".")).resolve()
            dockerfile = spec.get("dockerfile")
            df = context / (dockerfile or "Dockerfile")
            out = Path(tmp) / f"{name}.tar"
            raw_args = spec.get("args") or {}
            if isinstance(raw_args, list):
                raw_args = dict(str(a).split("=", 1) for a in raw_args if "=" in str(a))
            args = {str(k): str(v) for k, v in raw_args.items() if v is not None}
            cache = Path(os.environ["NOODLES_CACHE"]) / name if os.environ.get("NOODLES_CACHE") else None
            build(name, context, df if df.exists() else None, args, out, cache)
            if cache is not None:
                # 前回のキャッシュを今回書き出したもので置き換える
                shutil.rmtree(cache, ignore_errors=True)
                Path(f"{cache}.new").rename(cache)
            layout = Path(tmp) / name
            with tarfile.open(out) as t:
                t.extractall(layout, filter="data")
            ref = image_name(repo, str(os.environ.get("APP_PATH") or "."), name, commit)
            login(docker_config)
            run("crane", "push", str(layout), ref, env={"DOCKER_CONFIG": str(docker_config)})
            built += 1
    print(f"{built}個のイメージを push しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
