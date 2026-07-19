# frontend/

React + TypeScript + Vite PWA (scaffolded in T0.7+). Expected layout once filled in:

```
frontend/
  index.html
  vite.config.ts
  tsconfig.json
  package.json
  public/
    manifest.webmanifest
    icons/
  src/
    main.tsx
    App.tsx
    api/            # typed API client, /api/v1 base, CSRF handling
    screens/        # Login, AssetList, AssetDetail, ...
    components/
    hooks/
    sw.ts           # service worker (Milestone 4)
```

Stack: React + TypeScript, Vite, Mantine UI, @zxing/browser (QR scan, M4).
nginx serves the built bundle in production. See `../docs/architecture.md` and
`../docs/api-and-ui.md`.
