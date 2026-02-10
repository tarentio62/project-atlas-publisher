# Project Atlas Publisher

Script Python pour publier et structurer une collection de projets personnels sur GitHub (vitrine).

## Ce que fait le script
- Scanne chaque projet (structure, extensions, taille).
- Génère un `README.md` par projet via Gemini (si `GEMINI_API_KEY` est configurée).
- Détecte des secrets évidents avant publication (tokens GitHub, clés Google, clés AWS, blocs de clés privées).
- Publie les projets:
  - gros/projets "standalone" => un repo par projet
  - petits projets => regroupés dans un repo "vault" (archive)
  - un repo public "index" => ta vitrine (liens + stats)

## Setup
1. Crée un fichier `.env` à partir de `.env.example`
2. Variables minimales:
   - `GITHUB_USERNAME`
   - `GITHUB_TOKEN` (PAT avec droits repo)
3. Optionnel:
   - `GEMINI_API_KEY`

## Exécution
```powershell
python .\auto_publish_projects.py
```

## Noms de repos (branding)
Par défaut:
- `SHOWCASE_REPO_NAME=dev-portfolio-index`
- `ARCHIVE_REPO_NAME=dev-project-vault`

Tu peux les changer dans `.env`.

