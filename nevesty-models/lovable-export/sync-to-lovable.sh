#!/bin/bash
# =========================================================
# sync-to-lovable.sh
# Copies lovable-export files from Pablo repo → velvet-house
# Run this on your LOCAL machine where you have GitHub access
# =========================================================

set -e

PABLO_BRANCH="claude/modeling-agency-website-jp2Qd"
VELVET_REPO="https://github.com/smith77788/velvet-house-concierge-9fba01e9.git"
VELVET_DIR="velvet-house-concierge-9fba01e9"
PABLO_DIR="pablo-source"

echo "🔄 Syncing Pablo lovable-export → velvet-house-concierge..."

# 1. Clone Pablo source branch
if [ ! -d "$PABLO_DIR" ]; then
  echo "📥 Cloning Pablo repo (branch: $PABLO_BRANCH)..."
  git clone https://github.com/smith77788/Pablo.git \
    --branch "$PABLO_BRANCH" \
    --depth 1 \
    "$PABLO_DIR"
else
  echo "♻️  Updating Pablo repo..."
  git -C "$PABLO_DIR" pull origin "$PABLO_BRANCH"
fi

# 2. Clone velvet-house (Lovable project)
if [ ! -d "$VELVET_DIR" ]; then
  echo "📥 Cloning velvet-house repo..."
  git clone "$VELVET_REPO" "$VELVET_DIR"
else
  echo "♻️  Updating velvet-house repo..."
  git -C "$VELVET_DIR" pull origin main
fi

EXPORT_SRC="$PABLO_DIR/nevesty-models/lovable-export"
VELVET="$VELVET_DIR"

echo ""
echo "📋 Copying files..."

# 3. Copy Supabase migration (additive — only missing tables)
mkdir -p "$VELVET/supabase/migrations"
cp "$EXPORT_SRC/supabase/migrations/002_missing_features.sql" \
   "$VELVET/supabase/migrations/20240601000002_missing_features.sql"
echo "  ✓ supabase/migrations/002_missing_features.sql"

# 4. Copy Edge Functions
for fn in telegram-webhook send-sms send-email payment-webhook broadcast; do
  mkdir -p "$VELVET/supabase/functions/$fn"
  cp "$EXPORT_SRC/supabase/functions/$fn/index.ts" \
     "$VELVET/supabase/functions/$fn/index.ts"
  echo "  ✓ supabase/functions/$fn"
done

# 5. Copy React components
mkdir -p "$VELVET/src/components"
cp -r "$EXPORT_SRC/src/components/"* "$VELVET/src/components/"
echo "  ✓ src/components/ (admin, analytics, booking, catalog, payments, promo, reviews)"

# 6. Copy hooks
mkdir -p "$VELVET/src/hooks"
cp -r "$EXPORT_SRC/src/hooks/"* "$VELVET/src/hooks/"
echo "  ✓ src/hooks/"

# 7. Copy types
mkdir -p "$VELVET/src/types"
cp "$EXPORT_SRC/src/types/index.ts" "$VELVET/src/types/index.ts"
echo "  ✓ src/types/index.ts"

# 8. Copy Supabase client
mkdir -p "$VELVET/src/lib"
cp "$EXPORT_SRC/src/lib/supabase.ts" "$VELVET/src/lib/supabase.ts"
echo "  ✓ src/lib/supabase.ts"

# 9. Copy LOVABLE_PROMPT.md to root
cp "$PABLO_DIR/nevesty-models/LOVABLE_PROMPT.md" "$VELVET/LOVABLE_PROMPT.md"
echo "  ✓ LOVABLE_PROMPT.md"

# 10. Commit and push
echo ""
echo "📤 Committing changes to velvet-house..."
cd "$VELVET_DIR"
git add -A
git status --short

if git diff --staged --quiet; then
  echo "ℹ️  Nothing to commit — velvet-house is already up to date."
else
  git commit -m "feat: import Nevesty Models components, hooks, types, migrations, Edge Functions"
  git push origin main
  echo ""
  echo "✅ Done! Pushed to velvet-house-concierge-9fba01e9"
  echo ""
  echo "📌 Next steps:"
  echo "  1. In Supabase Studio → SQL Editor → run migration 20240601000002_missing_features.sql"
  echo "  2. Deploy Edge Functions: supabase functions deploy --project-ref YOUR_REF"
  echo "  3. Open Lovable.dev → paste LOVABLE_PROMPT.md into chat to build the UI"
fi
