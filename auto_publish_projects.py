

import os
import re
import json
import time
import shutil
import subprocess
from datetime import datetime
from collections import defaultdict

import requests
import google.generativeai as genai

# =====================================================
# CONFIG
# =====================================================

def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
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

STATE_FILE = os.path.join(WORKDIR, "state.json")

GEMINI_API_KEY = _env("GEMINI_API_KEY")
GITHUB_TOKEN = _env("GITHUB_TOKEN", required=True)
GITHUB_USERNAME = _env("GITHUB_USERNAME", required=True)

# Si True: supprime le repo GitHub existant puis le recrée (⚠️ destructif)
REPLACE_EXISTING = True

# Si True: force push (écrase l'historique remote) - utile si tu remplaces
FORCE_PUSH = True

API_TIMEOUT_SEC = 30
API_RETRY = 5
API_BACKOFF_BASE = 2

MIN_HOURS_STANDALONE = 80

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

# =====================================================
# GITHUB API
# =====================================================

def gh_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}

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

def github_create_repo(repo, private=True, description: str | None = None, topics: list[str] | None = None):
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

def create_or_replace_repo(repo, private=True, description: str | None = None, topics: list[str] | None = None):
    # s'assure que le nom est safe
    repo = slugify_repo_name(repo)

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
    # Prefer token in URL for non-interactive pushes.
    # If you have `gh auth setup-git`, you can switch to https://github.com/{user}/{repo}.git
    url = f"https://{GITHUB_USERNAME}:{GITHUB_TOKEN}@github.com/{GITHUB_USERNAME}/{repo}.git"
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

    if model is None:
        text = ""
    else:
        text = model.generate_content(prompt).text or ""

    readme = f"""# {analysis['project_name']}

{text}
{arch}
"""
    with open(os.path.join(dst, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme)


def write_root_readme(root_dir, stats, local_only):
    total_hours = sum(s["estimated_hours"] for s in stats)
    lines_local = "\n".join([f"- {p} — archive locale (environnement trop lourd / non versionné)" for p in local_only]) \
                  if local_only else "- (aucun pour l’instant)"

    root_readme = f"""# Dev Portfolio Index

Index de mes projets personnels: outils, prototypes, infra, R&D.
Objectif: publier proprement, éviter les leaks, et garder une trace exploitable dans le temps.

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

        analysis = analyze_with_gemini(structure)
        analysis["project_name"] = (analysis.get("project_name") or name).strip()

        # repo name safe
        repo_base = slugify_repo_name(analysis["project_name"])
        if analysis.get("public_recommendation") == "YES":
            repo_base = "public-" + repo_base

        # décision archive vs standalone
        hours = int(analysis.get("estimated_hours") or 0)
        is_standalone = hours >= MIN_HOURS_STANDALONE or analysis.get("complexity") != "LOW"

        if is_standalone:
            dst = os.path.join(WORKDIR, repo_base)
            # Ensure the publication copy starts clean to avoid mixing old files in WORKDIR
            if os.path.exists(dst):
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(full, dst, dirs_exist_ok=True)

            write_readme_project(dst, analysis,structure)

            # Prevent obvious leaks before publishing
            findings = scan_for_secrets(dst)
            if findings:
                log(f"Secrets détectés dans {name}: {len(findings)} finding(s). Projet mis en archive (pas de push).", "SECURITY")
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

            repo_final = create_or_replace_repo(repo_base, private=True, description=desc, topics=topics)

            # push
            git_commit_and_push(dst, repo_final)

            processed[name] = repo_final
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
        shutil.copytree(ppath, os.path.join(archive_dir, pname), dirs_exist_ok=True)

    archive_repo_final = create_or_replace_repo(ARCHIVE_REPO_NAME, private=True)
    git_commit_and_push(archive_dir, archive_repo_final)

    # =================================================
    # ROOT REPO (PUBLIC)
    # =================================================

    root_dir = os.path.join(WORKDIR, SHOWCASE_REPO_NAME)
    os.makedirs(root_dir, exist_ok=True)

    write_root_readme(root_dir, stats, local_only)

    root_topics = ["portfolio", "projects", "index", "showcase", "engineering"]
    root_repo_final = create_or_replace_repo(SHOWCASE_REPO_NAME, private=False, description="Index of my personal projects (auto-published, secrets-aware).", topics=root_topics)
    git_commit_and_push(root_dir, root_repo_final)

    log("=== END PROJECT PUBLISHER ===", "END")


if __name__ == "__main__":
    main()
