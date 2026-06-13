# Annotation Backoffice UI

React + Firebase backoffice for reviewing and annotating the `workflows-mods.jsonl` dataset.

## Prerequisites

- **Node.js 18+** — check with `node -v`. Install via [nodejs.org](https://nodejs.org) or `brew install node`.
- **npm 9+** — comes with Node. Check with `npm -v`.
- **Firebase CLI** — install once globally:
  ```bash
  npm install -g firebase-tools
  firebase login
  ```

## Setup

### 1. Create a Firebase project

1. Go to [console.firebase.google.com](https://console.firebase.google.com) and create a new project.
2. **Authentication**: Enable Google sign-in (Authentication → Sign-in method → Google).
3. **Firestore**: Create a database in Native mode.
4. **Hosting**: Enable Hosting.

### 2. Configure environment

Copy `.env.example` to `.env.local` and fill in your Firebase config values (Project Settings → Your apps → Web app → SDK snippet):

```bash
cp .env.example .env.local
# edit .env.local with your values
```

### 3. Install dependencies and run locally

```bash
npm install
npm run dev
# opens http://localhost:5173
```

### 4. Import data into Firestore

Download a service account key (Firebase Console → Project Settings → Service Accounts → Generate new private key) and save it as `service-account.json` in this directory (**never commit it**).

```bash
node scripts/import-samples.mjs
# or explicitly:
node scripts/import-samples.mjs ../data/zapier/workflows-mods.jsonl ./service-account.json
```

This is safe to re-run — it overwrites existing sample docs without touching annotations.

### 5. Deploy to Firebase Hosting

```bash
npm install -g firebase-tools
firebase login
firebase init   # select Hosting + Firestore, point public to dist/, SPA yes
npm run build
firebase deploy
```

## Keyboard shortcuts (detail page)

| Key | Action |
|-----|--------|
| `a` | Accept sample |
| `r` | Reject sample |
| `j` / `→` | Next sample |
| `k` / `←` | Previous sample |
| `Escape` | Blur active input (re-enable shortcuts) |

## Export annotations

On the list page, click **↓ Export Annotations** to download a JSONL file with all non-pending annotation verdicts merged with sample metadata.
