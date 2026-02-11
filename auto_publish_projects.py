

import os
import re
import json
import time
import shutil
import subprocess
from datetime import datetime
from collections import defaultdict
from typing import Optional

import requests
import google.generativeai as genai

# =====================================================
# CONFIG
# =====================================================

def _env(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    v = os.getenv(name, default)
    if required and not v:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return v

ROOT_PROJECTS_DIR = _env("ROOT_PROJECTS_DIR", r"C:\Users\nicol\Documents\projets perso")  # source folder
WORKDIR = _env("WORKDIR", "github_workspace")  # generated workspace

# Naming: one public "index/showcase" repo + one private/public archive repo for smaller projects.
# Pick a coherent branding; defaults are generic and "vitrine"-friendly.
SHOWCASE_REPO_NAME = _env("SHOWCASE_REPO_NAME", "dev-portfolio-index")
ARCHIVE_REPO_NAME = _env("ARCHIVE_REPO_NAME", "dev-project-vault")
GITLEAKS_PATH = _env("GITLEAKS_PATH", os.path.join(os.path.dirname(__file__), "tools", "bin", "gitleaks.exe"))

_client_kw = _env("CLIENT_PROJECT_KEYWORDS", "siyour,signee,mse,generali,prive,concierge,mfr,aplon") or ""
CLIENT_PROJECT_KEYWORDS = {k.strip().lower() for k in _client_kw.split(",") if k.strip()}

STATE_FILE = os.path.join(WORKDIR, "state.json")

GEMINI_API_KEY = _env("GEMINI_API_KEY")
GITHUB_TOKEN = _env("GITHUB_TOKEN")  # optional if using `gh` CLI auth
GITHUB_USERNAME = _env("GITHUB_USERNAME", required=True)

# Prefer GitHub CLI (uses your already-connected account). Set to 0 to force direct API via GITHUB_TOKEN.
USE_GH_CLI = (_env("USE_GH_CLI", "1") or "1").strip() in ("1", "true", "yes", "y", "on")

# If True: deletes existing GitHub repo then recreates (destructive)
REPLACE_EXISTING = (_env("REPLACE_EXISTING", "0") or "0").strip() in ("1", "true", "yes", "y", "on")

# If True: force push (overwrites remote history). Useful for re-import runs.
FORCE_PUSH = (_env("FORCE_PUSH", "1") or "1").strip() in ("1", "true", "yes", "y", "on")

API_TIMEOUT_SEC = 30
API_RETRY = 5
API_BACKOFF_BASE = 2

MIN_HOURS_STANDALONE = 80

# If True, projects recommended public by the LLM will be created as public (unless client/leaks).
ALLOW_PUBLIC = (_env("ALLOW_PUBLIC", "1") or "1").strip() in ("1", "true", "yes", "y", "on")
PUBLIC_POLICY = (_env("PUBLIC_POLICY", "llm") or "llm").strip().lower()
# PUBLIC_POLICY:
# - "llm": only public when Gemini recommends it
# - "safe": public when gitleaks is clean and not a client project (LLM optional)

# =====================================================
# PROJETS ARCHIVÉS LOCALEMENT (OPTION 3)
# =====================================================

LOCAL_ONLY_PROJECTS = {
    "EquinoxTest"
}

# =====================================================
# FILTRAGE
# =====================================================

IGNORED_DIRS = {
    ".git", "node_modules", "bin", "obj", "__pycache__",
    "dist", "build", "out", "output", "outputs",
    "venv", ".venv", ".idea", ".vs", ".vscode",
    "media", "medias", "images", "img", "videos",
    "data", "datasets", "logs", "temp", "tmp",
    ".ptvs"
}

IGNORED_EXT = {
    ".zip", ".rar", ".7z",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".mp4", ".avi", ".mkv",
    ".mp3", ".wav",
    ".exe", ".dll", ".class"
}

# =====================================================
# CLEANUP (safe, reversible only in WORKDIR copies)
# =====================================================

JUNK_DIRS = {
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

JUNK_FILE_PATTERNS = [
    re.compile(r".*\.log$", re.IGNORECASE),
    re.compile(r".*\.tmp$", re.IGNORECASE),
    re.compile(r".*\.bak$", re.IGNORECASE),
    re.compile(r".*\.swp$", re.IGNORECASE),
    re.compile(r"^Thumbs\.db$", re.IGNORECASE),
    re.compile(r"^\.DS_Store$", re.IGNORECASE),
]

def cleanup_tree_for_publish(path: str):
    """
    Deletes build/cache outputs in WORKDIR copies only (never touches ROOT_PROJECTS_DIR).
    """
    for root, dirs, files in os.walk(path):
        for d in list(dirs):
            if d in JUNK_DIRS:
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                dirs.remove(d)
        for fn in files:
            if any(rx.match(fn) for rx in JUNK_FILE_PATTERNS):
                try:
                    os.remove(os.path.join(root, fn))
                except:
                    pass

# =====================================================
# GEMINI
# =====================================================

model = None
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.5-flash-preview-09-2025")

# =====================================================
# LOGGING
# =====================================================

def log(msg, tag="INFO"):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{tag}] {msg}"
    print(line)
    os.makedirs(WORKDIR, exist_ok=True)
    with open(os.path.join(WORKDIR, "HISTORY.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")

# =====================================================
# STATE
# =====================================================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"processed": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except:
        s = {}
    if "processed" not in s:
        s["processed"] = {}
    return s

def save_state(state):
    os.makedirs(WORKDIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

# =====================================================
# UTILS
# =====================================================

def slugify_repo_name(name: str) -> str:
    """
    GitHub repo name safe:
    - lower
    - only a-z 0-9 . _ -
    - no spaces, no slashes, no parentheses
    - trimmed
    """
    s = name.strip().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[\/\\]+", "-", s)           # slash -> dash
    s = re.sub(r"[\(\)\[\]\{\}]+", "", s)    # remove brackets
    s = re.sub(r"[^a-z0-9\.\_\-\s]+", "", s) # remove weird chars
    s = re.sub(r"\s+", "-", s)              # spaces -> dash
    s = re.sub(r"-{2,}", "-", s)            # collapse dashes
    s = s.strip("-._")
    if not s:
        s = "project"
    if len(s) > 90:
        s = s[:90].rstrip("-._")
    return s

def safe_requests(method, url, headers=None, json_payload=None):
    for i in range(API_RETRY):
        try:
            r = requests.request(
                method,
                url,
                headers=headers,
                json=json_payload,
                timeout=API_TIMEOUT_SEC
            )
            return r
        except requests.exceptions.RequestException as e:
            wait = API_BACKOFF_BASE ** i
            log(f"Erreur réseau: {e} | retry dans {wait}s", "WARN")
            time.sleep(wait)
    raise RuntimeError("Erreur réseau persistante (GitHub API)")

# =====================================================
# CLASSIFICATION HELPERS
# =====================================================

def is_client_project(project_folder_name: str) -> bool:
    n = (project_folder_name or "").lower()
    return any(k in n for k in CLIENT_PROJECT_KEYWORDS)

# =====================================================
# GEMINI CALL
# =====================================================

def call_gemini_json(prompt: str) -> dict:
    if model is None:
        raise RuntimeError("GEMINI_API_KEY is not set; cannot call Gemini.")
    response = model.generate_content(prompt)
    text = (response.text or "").replace("```json", "").replace("```", "").strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Gemini n'a pas retourné de JSON valide")

    return json.loads(text[start:end + 1])

# =====================================================
# SCAN
# =====================================================

def scan_project(path):
    files = []
    ext_count = defaultdict(int)
    size = 0

    for root, dirs, filenames in os.walk(path):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]

        for f in filenames:
            ext = os.path.splitext(f)[1].lower()
            if ext in IGNORED_EXT:
                continue

            full = os.path.join(root, f)
            try:
                size += os.path.getsize(full)
            except:
                continue

            rel = os.path.relpath(full, path)
            files.append(rel)
            ext_count[ext] += 1

    return {
        "file_count": len(files),
        "size_kb": size // 1024,
        "extensions": dict(ext_count),
        "sample_files": files[:80]
    }

# =====================================================
# ANALYSE (FR)
# =====================================================

def analyze_with_gemini(structure):
    prompt = f"""
Tu es un architecte logiciel senior.

Retourne STRICTEMENT un JSON avec:
- project_name (nom court propre)
- estimated_hours (nombre)
- complexity (LOW | MEDIUM | HIGH)
- project_nature (tool | product | service | infra | r&d)
- public_recommendation (YES | NO)
- reasoning (français, 2-5 lignes max)

Structure:
{json.dumps(structure, indent=2, ensure_ascii=False)}
"""
    return call_gemini_json(prompt)

def infer_topics_from_structure(structure: dict) -> list[str]:
    exts = structure.get("extensions", {}) or {}
    topics = set(["portfolio", "automation", "publisher"])

    if ".py" in exts:
        topics.update(["python"])
    if ".js" in exts or ".ts" in exts:
        topics.update(["javascript"])
    if ".cs" in exts:
        topics.update(["dotnet", "csharp"])
    if ".go" in exts:
        topics.update(["golang"])
    if ".rs" in exts:
        topics.update(["rust"])
    if ".java" in exts:
        topics.update(["java"])
    if ".php" in exts:
        topics.update(["php"])
    if ".tf" in exts:
        topics.update(["terraform", "infra"])
    if ".yml" in exts or ".yaml" in exts:
        topics.update(["devops"])

    return sorted(topics)[:20]

# =====================================================
# SECRET SCAN (minimal, conservative)
# =====================================================

SECRET_PATTERNS = [
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "github_token_ghp"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github_pat"),
    (re.compile(r"AIzaSy[0-9A-Za-z\-_]{20,}"), "google_api_key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws_access_key_id"),
    (re.compile(r"-----BEGIN (RSA|OPENSSH|EC) PRIVATE KEY-----"), "private_key_block"),
]

def scan_for_secrets(project_path: str, max_files: int = 8000, max_bytes_per_file: int = 512_000) -> list[dict]:
    """
    Fast heuristic scan to prevent obvious leaks.
    Returns a list of findings with (kind, file, match_snippet).
    """
    findings = []
    count = 0
    for root, dirs, filenames in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for fn in filenames:
            count += 1
            if count > max_files:
                findings.append({"kind": "scan_limit", "file": "", "match_snippet": f"Stopped after {max_files} files"})
                return findings

            ext = os.path.splitext(fn)[1].lower()
            if ext in IGNORED_EXT:
                continue

            full = os.path.join(root, fn)
            try:
                if os.path.getsize(full) > max_bytes_per_file:
                    continue
                data = open(full, "rb").read()
            except:
                continue

            # Avoid noisy binary scanning; still catch PEM blocks in text-ish blobs
            try:
                text = data.decode("utf-8", errors="ignore")
            except:
                continue

            for rx, kind in SECRET_PATTERNS:
                m = rx.search(text)
                if not m:
                    continue
                rel = os.path.relpath(full, project_path)
                snippet = text[max(0, m.start() - 20):m.end() + 20].replace("\n", "\\n")
                findings.append({"kind": kind, "file": rel, "match_snippet": snippet[:160]})
    return findings

def run_gitleaks(project_path: str, report_path: str) -> tuple[bool, str]:
    """
    Returns (has_leaks, stderr_tail).
    Uses --redact=100 to avoid printing secrets.
    """
    exe = GITLEAKS_PATH
    if not exe or not os.path.exists(exe):
        return False, "gitleaks not found"

    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    # --no-git: scan directory contents as-is.
    # Exit code 1 means leaks found.
    p = subprocess.run(
        [exe, "detect", "--no-banner", "--redact", "100", "--no-git", "-s", project_path, "-f", "json", "-r", report_path],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    stderr_tail = (p.stderr or "")[-500:]
    if p.returncode == 1:
        return True, stderr_tail
    if p.returncode == 0:
        return False, stderr_tail
    # Non-standard error; treat as "cannot certify".
    return True, f"gitleaks error (rc={p.returncode}): {stderr_tail}"

# =====================================================
# GITHUB API
# =====================================================

def gh_headers():
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is not set (and USE_GH_CLI=0).")
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}

def run_gh(args: list[str], cwd: Optional[str] = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["gh"] + args, cwd=cwd, check=check, capture_output=True, text=True)

def gh_cli_repo_exists(repo: str) -> bool:
    r = run_gh(["repo", "view", f"{GITHUB_USERNAME}/{repo}"], check=False)
    return r.returncode == 0

def gh_cli_delete_repo(repo: str):
    log(f"Suppression du repo existant: {repo}", "GITHUB")
    run_gh(["repo", "delete", f"{GITHUB_USERNAME}/{repo}", "--yes"], check=True)

def gh_cli_create_repo(repo: str, private: bool, description: Optional[str] = None):
    args = ["repo", "create", repo, "--confirm"]
    args.append("--private" if private else "--public")
    if description:
        args += ["--description", description[:160]]
    run_gh(args, check=True)

def gh_cli_set_topics(repo: str, topics: list[str]):
    for t in [x.strip().lower() for x in topics if x.strip()]:
        run_gh(["repo", "edit", f"{GITHUB_USERNAME}/{repo}", "--add-topic", t], check=False)

def github_repo_exists(repo):
    url = f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo}"
    r = safe_requests("GET", url, headers=gh_headers())
    return r.status_code == 200

def github_delete_repo(repo):
    url = f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo}"
    log(f"Suppression du repo existant: {repo}", "GITHUB")
    r = safe_requests("DELETE", url, headers=gh_headers())
    if r.status_code not in (204, 404):
        raise RuntimeError(r.text)

def github_create_repo(repo, private=True, description: Optional[str] = None, topics: Optional[list[str]] = None):
    url = "https://api.github.com/user/repos"
    payload = {"name": repo, "private": private}
    if description:
        payload["description"] = description[:160]
    log(f"Création du repo: {repo} (private={private})", "GITHUB")
    r = safe_requests("POST", url, headers=gh_headers(), json_payload=payload)
    if r.status_code == 201:
        if topics:
            github_set_topics(repo, topics)
        return
    if r.status_code == 422:
        # already exists
        if topics:
            github_set_topics(repo, topics)
        return
    raise RuntimeError(r.text)

def github_set_topics(repo: str, topics: list[str]):
    # GitHub requires a custom media type for topics.
    url = f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo}/topics"
    headers = gh_headers()
    headers["Accept"] = "application/vnd.github+json"
    payload = {"names": [t.strip().lower() for t in topics if t.strip()]}
    safe_requests("PUT", url, headers=headers, json_payload=payload)

def create_or_replace_repo(repo, private=True, description: Optional[str] = None, topics: Optional[list[str]] = None):
    # s'assure que le nom est safe
    repo = slugify_repo_name(repo)

    # Prefer gh CLI when available: uses existing auth, avoids embedding tokens anywhere.
    if USE_GH_CLI:
        if gh_cli_repo_exists(repo):
            if REPLACE_EXISTING:
                gh_cli_delete_repo(repo)
                time.sleep(1)
                gh_cli_create_repo(repo, private=private, description=description)
            else:
                log(f"Repo déjà existant, on garde: {repo}", "GITHUB")
        else:
            gh_cli_create_repo(repo, private=private, description=description)

        if topics:
            gh_cli_set_topics(repo, topics)
        return repo

    if github_repo_exists(repo):
        if REPLACE_EXISTING:
            github_delete_repo(repo)
            time.sleep(1)  # petite pause pour propagation
            github_create_repo(repo, private=private, description=description, topics=topics)
        else:
            log(f"Repo déjà existant, on garde: {repo}", "GITHUB")
    else:
        github_create_repo(repo, private=private, description=description, topics=topics)

    return repo

# =====================================================
# GIT HELPERS (robustes)
# =====================================================

def run_git(args, cwd, check=True):
    return subprocess.run(["git"] + args, cwd=cwd, check=check)

def ensure_git_repo(path):
    if not os.path.exists(os.path.join(path, ".git")):
        run_git(["init"], cwd=path, check=True)

def set_remote_origin(path, repo):
    # Use plain HTTPS remote; auth is handled by Git credential helper / GitHub CLI.
    url = f"https://github.com/{GITHUB_USERNAME}/{repo}.git"
    # si origin existe -> set-url ; sinon add
    remotes = subprocess.run(["git", "remote"], cwd=path, capture_output=True, text=True).stdout.split()
    if "origin" in remotes:
        run_git(["remote", "set-url", "origin", url], cwd=path, check=True)
    else:
        run_git(["remote", "add", "origin", url], cwd=path, check=True)

def git_commit_and_push(path, repo):
    ensure_git_repo(path)
    set_remote_origin(path, repo)

    # Important sur Windows: évite les erreurs de chemins trop longs côté git add
    # On ajoute seulement ce qui est déjà filtré par tes ignores + dossier nettoyé
    run_git(["add", "."], cwd=path, check=False)
    run_git(["commit", "-m", "Import initial"], cwd=path, check=False)
    run_git(["branch", "-M", "main"], cwd=path, check=False)

    push_args = ["push", "-u", "origin", "main"]
    if FORCE_PUSH:
        push_args.insert(1, "--force")
    run_git(push_args, cwd=path, check=True)

# =====================================================
# README HELPERS
# =====================================================
def write_readme_project(dst, analysis, structure):
    arch = ""
    if len(structure.get("extensions", {})) > 2:
        arch = "\n## Architecture\n" + "\n".join(
            f"- {ext.replace('.', '')}" for ext in structure["extensions"].keys()
        )

    if model is None:
        exts = ", ".join(sorted([e.lstrip(".") for e in (structure.get("extensions", {}) or {}).keys() if e]))
        sample = structure.get("sample_files", []) or []
        hint = "\n".join([f"- `{p}`" for p in sample[:10]]) if sample else "- (no sample files captured)"
        text = f"""## Resume
Projet importe depuis une archive personnelle.

## Tech
- Files: {structure.get('file_count', 0)}
- Size: {structure.get('size_kb', 0)} KB
- Extensions: {exts or 'n/a'}

## Start
Voir les fichiers du projet et lancer selon la stack (README auto minimal, sans LLM).

## Files (sample)
{hint}

### English
Imported from a personal archive. Minimal auto-generated README (no LLM configured).
"""
    else:
        prompt = f"""
Tu es un architecte logiciel senior.

Rédige un README COURT et naturel pour un projet logiciel.

Contraintes STRICTES :
- max 120 mots en français
- pas de marketing
- pas de blabla
- ton professionnel
- explique ce que fait le projet
- si possible comment le lancer

Puis ajoute une section English (max 60 mots).

Nom du projet : {analysis['project_name']}
Nature : {analysis['project_nature']}
Complexité : {analysis['complexity']}
"""
        text = model.generate_content(prompt).text or ""

    readme = f"""# {analysis['project_name']}

{text}
{arch}
"""
    with open(os.path.join(dst, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme)


def write_root_readme(root_dir, stats, local_only, public_repos):
    total_hours = sum(s["estimated_hours"] for s in stats)
    lines_local = "\n".join([f"- {p} — archive locale (environnement trop lourd / non versionné)" for p in local_only]) \
                  if local_only else "- (aucun pour l’instant)"

    featured = []
    for item in public_repos:
        # item: dict(repo, analysis)
        a = item.get("analysis", {}) or {}
        featured.append(
            (
                int(a.get("estimated_hours") or 0),
                item.get("repo", ""),
                a.get("project_name") or item.get("repo", ""),
                a.get("project_nature") or "project",
                a.get("complexity") or "",
            )
        )
    featured.sort(key=lambda x: x[0], reverse=True)
    featured_lines = "\n".join(
        [f"- **{name}** ({hours}h, {nature}, {cx})  \n  `https://github.com/{GITHUB_USERNAME}/{repo}`"
         for hours, repo, name, nature, cx in featured[:30]]
    ) if featured else "- (no public repos published by this run)"

    root_readme = f"""# Dev Portfolio Index

Index de mes projets personnels: outils, prototypes, infra, R&D.
Objectif: publier proprement, éviter les leaks, et garder une trace exploitable dans le temps.

## Featured (public)
{featured_lines}

## Statistiques globales
- Projets analysés : {len(stats)}
- Temps estimé cumulé : **{total_hours} h**

## Projets archivés localement (non versionnés)
{lines_local}

---

### English help
This repository is the index of my personal projects.
French-first notes, with short English sections when useful.
"""
    with open(os.path.join(root_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write(root_readme)

# =====================================================
# MAIN
# =====================================================

def main():
    log("=== START PROJECT PUBLISHER ===", "START")
    os.makedirs(WORKDIR, exist_ok=True)

    state = load_state()
    processed = state["processed"]

    stats = []
    local_only = []
    archive_projects = []
    public_repos = []

    for name in os.listdir(ROOT_PROJECTS_DIR):
        full = os.path.join(ROOT_PROJECTS_DIR, name)
        if not os.path.isdir(full):
            continue

        if name in processed:
            log(f"Déjà traité : {name}", "SKIP")
            continue

        if name in LOCAL_ONLY_PROJECTS:
            log(f"Archivé localement (option 3) : {name}", "ARCHIVE")
            processed[name] = "local_archive"
            local_only.append(name)
            save_state(state)
            continue

        log(f"Analyse du projet : {name}", "MEMORY")

        structure = scan_project(full)
        if structure["file_count"] == 0:
            log("Projet vide / ignoré", "SKIP")
            processed[name] = "empty"
            save_state(state)
            continue

        if model is None:
            analysis = {
                "project_name": name.strip(),
                "estimated_hours": 0,
                "complexity": "LOW",
                "project_nature": "tool",
                "public_recommendation": "NO",
                "reasoning": "GEMINI_API_KEY non configurée.",
            }
        else:
            analysis = analyze_with_gemini(structure)
            analysis["project_name"] = (analysis.get("project_name") or name).strip()

        # repo name safe
        repo_base = slugify_repo_name(analysis["project_name"])
        forced_private = is_client_project(name)

        # décision archive vs standalone
        hours = int(analysis.get("estimated_hours") or 0)
        is_standalone = hours >= MIN_HOURS_STANDALONE or analysis.get("complexity") != "LOW"

        if is_standalone:
            dst = os.path.join(WORKDIR, repo_base)
            # Ensure the publication copy starts clean to avoid mixing old files in WORKDIR
            if os.path.exists(dst):
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(full, dst, dirs_exist_ok=True)
            cleanup_tree_for_publish(dst)

            write_readme_project(dst, analysis,structure)

            # Prevent obvious leaks before publishing
            gitleaks_report = os.path.join(WORKDIR, "_gitleaks_reports", f"{repo_base}.json")
            has_leaks, gl_err = run_gitleaks(dst, gitleaks_report)
            if has_leaks:
                log(f"Gitleaks: leak(s) or scan error for {name}. Kept private/archive. ({gl_err})", "SECURITY")
                processed[name] = "gitleaks_blocked"
                archive_projects.append((name, full))
                stats.append(analysis)
                save_state(state)
                continue

            # Extra heuristic: catch obvious tokens even if gitleaks misses it.
            findings = scan_for_secrets(dst)
            if findings:
                log(f"Secrets détectés dans {name}: {len(findings)} finding(s). Kept private/archive.", "SECURITY")
                processed[name] = "secret_detected_archive"
                archive_projects.append((name, full))
                stats.append(analysis)
                save_state(state)
                continue

            # create/replace repo
            desc = f"{analysis.get('project_nature','project')} | complexity {analysis.get('complexity','')} | auto-published from personal archive"
            topics = infer_topics_from_structure(structure)
            if analysis.get("public_recommendation") == "YES":
                topics = sorted(set(topics + ["public"]))

            if PUBLIC_POLICY == "safe":
                make_public = ALLOW_PUBLIC and (not forced_private)
            else:
                make_public = ALLOW_PUBLIC and (analysis.get("public_recommendation") == "YES") and (not forced_private)
            if forced_private:
                topics = sorted(set(topics + ["client", "private"]))
            repo_final = create_or_replace_repo(repo_base, private=(not make_public), description=desc, topics=topics)

            # push
            git_commit_and_push(dst, repo_final)

            processed[name] = repo_final
            if make_public:
                public_repos.append({"repo": repo_final, "analysis": analysis})
        else:
            archive_projects.append((name, full))
            processed[name] = "archive"

        stats.append(analysis)
        save_state(state)

    # =================================================
    # ARCHIVE REPO
    # =================================================

    archive_dir = os.path.join(WORKDIR, ARCHIVE_REPO_NAME)
    os.makedirs(archive_dir, exist_ok=True)

    for pname, ppath in archive_projects:
        dst = os.path.join(archive_dir, pname)
        if os.path.exists(dst):
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(ppath, dst, dirs_exist_ok=True)
        cleanup_tree_for_publish(dst)

    archive_repo_final = create_or_replace_repo(ARCHIVE_REPO_NAME, private=True)
    git_commit_and_push(archive_dir, archive_repo_final)

    # =================================================
    # ROOT REPO (PUBLIC)
    # =================================================

    root_dir = os.path.join(WORKDIR, SHOWCASE_REPO_NAME)
    os.makedirs(root_dir, exist_ok=True)

    write_root_readme(root_dir, stats, local_only, public_repos)

    root_topics = ["portfolio", "projects", "index", "showcase", "engineering"]
    root_repo_final = create_or_replace_repo(SHOWCASE_REPO_NAME, private=False, description="Index of my personal projects (auto-published, secrets-aware).", topics=root_topics)
    git_commit_and_push(root_dir, root_repo_final)

    log("=== END PROJECT PUBLISHER ===", "END")


if __name__ == "__main__":
    main()
