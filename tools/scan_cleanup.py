import argparse
import json
import os
import re
import shutil
from dataclasses import dataclass, asdict
from datetime import datetime


SECRET_REGEXES = [
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "github_token_ghp"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github_pat"),
    (re.compile(r"AIzaSy[0-9A-Za-z\\-_]{20,}"), "google_api_key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws_access_key_id"),
    (re.compile(r"-----BEGIN (RSA|OPENSSH|EC) PRIVATE KEY-----"), "private_key_block"),
]


SAFE_JUNK_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    "node_modules",
    "dist",
    "build",
    "out",
    "bin",
    "obj",
    "target",
    ".venv",
    "venv",
    "coverage",
}

SAFE_JUNK_FILES = [
    re.compile(r".*\\.log$", re.IGNORECASE),
    re.compile(r".*\\.tmp$", re.IGNORECASE),
    re.compile(r".*\\.bak$", re.IGNORECASE),
    re.compile(r".*\\.swp$", re.IGNORECASE),
    re.compile(r"^Thumbs\\.db$", re.IGNORECASE),
    re.compile(r"^\\.DS_Store$", re.IGNORECASE),
]


def is_probably_binary(data: bytes) -> bool:
    if not data:
        return False
    # Heuristic: NUL bytes or high non-text ratio.
    if b"\x00" in data:
        return True
    sample = data[:4096]
    nontext = sum(1 for b in sample if b < 9 or (b > 13 and b < 32))
    return (nontext / max(1, len(sample))) > 0.30


def scan_file_for_secrets(path: str, max_bytes: int = 512_000) -> list[dict]:
    try:
        if os.path.getsize(path) > max_bytes:
            return []
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return []

    if is_probably_binary(data):
        # Still attempt PEM detection in case it's embedded.
        try:
            text = data.decode("utf-8", errors="ignore")
        except Exception:
            return []
    else:
        try:
            text = data.decode("utf-8", errors="ignore")
        except Exception:
            return []

    findings = []
    for rx, kind in SECRET_REGEXES:
        m = rx.search(text)
        if not m:
            continue
        snippet = text[max(0, m.start() - 20) : m.end() + 20].replace("\n", "\\n")
        findings.append({"kind": kind, "match_snippet": snippet[:160]})
    return findings


def dir_size(path: str) -> int:
    total = 0
    for root, dirs, files in os.walk(path):
        for fn in files:
            fp = os.path.join(root, fn)
            try:
                total += os.path.getsize(fp)
            except Exception:
                pass
    return total


@dataclass
class ProjectReport:
    name: str
    path: str
    secret_findings: int
    junk_paths: int
    junk_bytes: int
    public_candidate: bool
    notes: list


def looks_like_personal_data_project(name: str, root: str) -> bool:
    # Conservative flags: anything that looks like inventory, dumps, exports, backups.
    lowered = name.lower()
    if any(k in lowered for k in ["backup", "dump", "export", "invoice", "facture", "client", "customer", "crm"]):
        return True

    # If the project has a top-level data-ish directory, treat as risky by default.
    for d in ["data", "dataset", "datasets", "exports", "dump", "dumps", "backups", "backup"]:
        if os.path.isdir(os.path.join(root, d)):
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="Root folder containing projects (subdirectories).")
    ap.add_argument("--quarantine", required=True, help="Where to move junk for recovery.")
    ap.add_argument("--apply", action="store_true", help="Actually move junk to quarantine.")
    ap.add_argument("--max-projects", type=int, default=0, help="Optional limit for testing.")
    ap.add_argument("--out", default="", help="Write JSON report to this path.")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    quarantine = os.path.abspath(args.quarantine)
    os.makedirs(quarantine, exist_ok=True)

    projects = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
    projects.sort(key=lambda s: s.lower())
    if args.max_projects and args.max_projects > 0:
        projects = projects[: args.max_projects]

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    report = []

    for name in projects:
        p = os.path.join(root, name)
        secret_findings = 0
        junk = []
        junk_bytes = 0
        notes = []

        # Secret scan (fast and heuristic)
        for r, dirs, files in os.walk(p):
            # Skip junk dirs for speed
            dirs[:] = [d for d in dirs if d not in SAFE_JUNK_DIRS]
            for fn in files:
                fp = os.path.join(r, fn)
                rel = os.path.relpath(fp, p)
                # Avoid scanning common large binaries
                ext = os.path.splitext(fn)[1].lower()
                if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".avi", ".mkv", ".mp3", ".wav", ".zip", ".7z", ".rar", ".exe", ".dll"}:
                    continue
                fnds = scan_file_for_secrets(fp)
                if fnds:
                    secret_findings += len(fnds)

        # Junk discovery (safe rules only)
        for r, dirs, files in os.walk(p):
            for d in list(dirs):
                if d in SAFE_JUNK_DIRS:
                    full = os.path.join(r, d)
                    junk.append(os.path.relpath(full, p))
                    try:
                        junk_bytes += dir_size(full)
                    except Exception:
                        pass
                    dirs.remove(d)
            for fn in files:
                if any(rx.match(fn) for rx in SAFE_JUNK_FILES):
                    full = os.path.join(r, fn)
                    junk.append(os.path.relpath(full, p))
                    try:
                        junk_bytes += os.path.getsize(full)
                    except Exception:
                        pass

        risky = looks_like_personal_data_project(name, p)
        if risky:
            notes.append("risk: personal-data-like naming or data/exports folders detected (conservative)")

        public_candidate = (secret_findings == 0) and (not risky)

        if args.apply and junk:
            qdir = os.path.join(quarantine, run_id, name)
            os.makedirs(qdir, exist_ok=True)
            for rel in junk:
                src = os.path.join(p, rel)
                dst = os.path.join(qdir, rel.replace("\\", "__").replace("/", "__"))
                try:
                    if os.path.isdir(src):
                        shutil.move(src, dst)
                    elif os.path.isfile(src):
                        shutil.move(src, dst)
                except Exception:
                    # Best-effort cleanup; keep going.
                    pass

        report.append(
            ProjectReport(
                name=name,
                path=p,
                secret_findings=secret_findings,
                junk_paths=len(junk),
                junk_bytes=junk_bytes,
                public_candidate=public_candidate,
                notes=notes,
            )
        )

    out_obj = {
        "root": root,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "projects": [asdict(r) for r in report],
    }

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out_obj, f, indent=2, ensure_ascii=False)
    else:
        print(json.dumps(out_obj, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

