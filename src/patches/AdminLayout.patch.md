# Patch: Додати Pablo AI в AdminLayout.tsx

## У файлі: `src/components/AdminLayout.tsx`

### 1. Знайди масив `navGroups` та групу `"🤖 AI-агенти"`:

```typescript
{
  label: "🤖 AI-агенти",
  icon: Sparkles,
  items: [
    { to: "/admin/agents", icon: Activity, label: "Стан помічників" },
    ...
  ],
},
```

### 2. Додай Pablo AI як ПЕРШУ групу у навігації (перед усіма іншими):

```typescript
{
  label: "🧠 Pablo AI",
  icon: Brain,
  items: [
    { to: "/admin/pablo-ai", icon: Brain, label: "Pablo AI — Мозок бізнесу" },
    { to: "/admin/pablo-approvals", icon: Shield, label: "Підтвердження рішень", badge: "pablo_approvals" },
  ],
},
```

### 3. Додай `pablo_approvals` badge до типу NavItem:

```typescript
type NavItem = { to: string; icon: any; label: string; badge?: "chats" | "notifications" | "distributors" | "pablo_approvals" };
```

### 4. Додай import для Shield та Brain (якщо ще немає):
```typescript
import { Brain, Shield } from "lucide-react";
```

---

## У файлі: `src/App.tsx`

### 1. Додай lazy import:
```typescript
const AdminPabloAI = lazy(() => import("./pages/admin/AdminPabloAI"));
```

### 2. Додай route в блок /admin:
```tsx
<Route path="pablo-ai" element={<ErrorBoundary label="Pablo AI"><AdminPabloAI /></ErrorBoundary>} />
```
