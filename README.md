# coworking-sens.com — Site de réservation

L'Atelier du Coworking Sens — 20 rue Pasteur, 89100 Sens.

## Stack

- Next.js 15 (App Router)
- React 19
- TypeScript
- Tailwind CSS
- Déployé sur Vercel
- Backend FastAPI sur `pole-iad-sens.fr` (à brancher)

## Structure

```
app/
  page.tsx                          Home avec hero + cards d'espaces
  reserver/page.tsx                 Tunnel de réservation 3 étapes
  reservation/confirmation/page.tsx Page après paiement
  devis-privatisation/page.tsx      Formulaire de devis pour privatisation
  layout.tsx
  globals.css
components/
  Header.tsx
  Footer.tsx
lib/
  spaces.ts                         Catalogue espaces + tarifs
```

## Lancer en local

```bash
npm install
npm run dev
```

Ouvre http://localhost:3000

## Déployer sur Vercel

Push sur la branche `main` du repo `poledeformationsens/coworking-sens-com` →
Vercel redéploie automatiquement.

## Variables d'environnement à ajouter sur Vercel

```
NEXT_PUBLIC_API_URL=https://pole-iad-sens.fr/api/v1
```

## À faire

- [ ] Brancher les endpoints FastAPI dans `/reserver` et `/devis-privatisation`
- [ ] Intégrer Stripe Checkout
- [ ] Ajouter page `/mon-espace` (espace client avec historique)
- [ ] Auth magic-link
- [ ] Composant Calendar avec disponibilités en temps réel
- [ ] SIRET autocomplete via API Recherche Entreprises
