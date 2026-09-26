# StockSense Backend — Starter

## Local setup (no cloud dependency — works fully offline once installed)

1. Install Postgres locally (or run it in Docker) and create a database
   named `stocksense`.
2. `cp .env.example .env` and fill in your local DB credentials.
3. `npm install`
4. `npx prisma migrate dev --name init` — creates tables from schema.prisma
5. `npm run seed` — populates realistic demo data (walks through the
   exact Receive → Transfer → Deliver → Adjust example from the problem
   statement)
6. `npm run dev` — starts the API on http://localhost:4000
7. Check `http://localhost:4000/api/health` — should return
   `{ status: "ok", db: "connected" }`

Demo login after seeding: `manager@stocksense.dev` / `password123`

## Git workflow (everyone commits, not just one person)

- Each teammate clones the repo and works on their own branch:
  - `feature/auth`
  - `feature/products`
  - `feature/stock-moves` (receipts, deliveries, transfers, adjustments)
  - `feature/dashboard`
  - `feature/frontend-ui`
- Commit early and often under your own account — small, frequent
  commits with clear messages, not one giant end-of-day commit.
- Open a PR into `main` when a feature works locally. Merge conflicts
  are much easier to resolve in small PRs than in one big merge at
  hour 11.
- Don't push directly to `main` if you can help it — even a quick
  self-review before merging shows in the PR history.

## Why the schema looks the way it does

Every stock-changing action (receipt, delivery, transfer, adjustment)
creates one or more `StockMove` rows, and *only* StockMove-adjacent code
paths update `Product.qtyOnHand`. This means:

- The stock ledger requirement is automatic — you never have to
  remember to "also log this" in a separate step.
- There's exactly one place to add validation (quantity > 0, sufficient
  stock before a delivery, product/warehouse exists) rather than four
  separate ones.

When you build the receipt/delivery/transfer/adjustment endpoints next,
each one should:
1. Validate input (product exists, quantity > 0, sufficient stock for
   deliveries/transfers)
2. Create the StockMove row(s) inside a Prisma transaction together
   with the `Product.qtyOnHand` update, so a crash mid-operation can't
   leave stock and ledger out of sync
3. Return the updated product + move for the frontend to reflect
   immediately
