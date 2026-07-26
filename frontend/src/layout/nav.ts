import {
  IconBell,
  IconBox,
  IconBriefcase,
  IconCalendarEvent,
  IconCategory,
  IconClipboardList,
  IconFileImport,
  IconHistory,
  IconLayoutDashboard,
  IconMail,
  IconMapPin,
  IconPackages,
  IconPrinter,
  IconQrcode,
  IconUserCircle,
  IconUserCog,
  IconUsers,
  type Icon,
} from "@tabler/icons-react";
import type { Me } from "../api/types";
import {
  LABEL_GENERATE,
  TENANT_MANAGE,
  hasAnyAssetPermission,
  hasAuditViewPermission,
  hasImportRunPermission,
  hasPermission,
  hasProjectViewPermission,
  hasUserManagePermission,
} from "../api/permissions";

export interface NavItem {
  to: string;
  label: string;
  icon: Icon;
  testId?: string;
  /** Presentation-only gate, mirroring exactly what the pre-redesign
   * Dashboard button stack showed/hid (CLAUDE.md: never the security
   * boundary — the server re-checks). `undefined` = always shown. */
  hidden?: (me: Me | null | undefined) => boolean;
  /** Desktop sidebar section caption this item renders under (see
   * `AppLayout`'s grouping). `undefined` = ungrouped, rendered above every
   * section with no caption (just Dashboard). Purely visual — has no bearing
   * on the mobile tab bar / "More" sheet, which stay flat lists. */
  section?: "Workflow" | "You" | "Tools" | "Admin";
}

/**
 * The full set of top-level destinations (~10 sections), in the order the
 * desktop sidebar renders them. Every route in `App.tsx` is reachable from
 * this list — the mobile bottom-tab bar (`MOBILE_TABS` below) only ever
 * surfaces a subset, with the rest reachable through the "More" sheet, so
 * nothing here becomes unreachable on a phone.
 */
export const NAV_ITEMS: NavItem[] = [
  // Overview
  { to: "/", label: "Dashboard", icon: IconLayoutDashboard, testId: "nav-dashboard" },
  // Core inventory workflow, in the order a lab tech actually moves through
  // it: find/browse an asset, scan its QR label, track consumable stock,
  // reserve time on gear, and approve/route those requests.
  { to: "/assets", label: "Assets", icon: IconBox, testId: "nav-assets", section: "Workflow" },
  { to: "/scan", label: "Scan", icon: IconQrcode, testId: "nav-scan", section: "Workflow" },
  { to: "/stock", label: "Stock", icon: IconPackages, testId: "nav-stock", section: "Workflow" },
  {
    to: "/reservations",
    label: "Reservations",
    icon: IconCalendarEvent,
    testId: "nav-reservations",
    section: "Workflow",
  },
  {
    to: "/approvals",
    label: "Approvals",
    icon: IconClipboardList,
    testId: "nav-approvals",
    section: "Workflow",
  },
  // Personal / project-scoped views
  { to: "/my-items", label: "My Items", icon: IconUserCircle, testId: "nav-my-items", section: "You" },
  {
    to: "/projects",
    label: "Projects",
    icon: IconBriefcase,
    testId: "nav-projects",
    hidden: (me) => !hasProjectViewPermission(me),
    section: "You",
  },
  { to: "/notifications", label: "Notifications", icon: IconBell, testId: "nav-notifications", section: "You" },
  // Every signed-in user can reach their own account to change their password
  // (no permission gate — self-service).
  { to: "/account", label: "Account", icon: IconUserCog, testId: "nav-account", section: "You" },
  // Operational tools
  {
    to: "/labels",
    label: "Print Labels",
    icon: IconPrinter,
    testId: "nav-labels",
    hidden: (me) => !hasAnyAssetPermission(me, LABEL_GENERATE),
    section: "Tools",
  },
  {
    to: "/import",
    label: "Bulk Import",
    icon: IconFileImport,
    testId: "nav-import",
    hidden: (me) => !hasImportRunPermission(me),
    section: "Tools",
  },
  {
    to: "/audit",
    label: "Audit Log",
    icon: IconHistory,
    testId: "nav-audit",
    hidden: (me) => !hasAuditViewPermission(me),
    section: "Tools",
  },
  // Tenant admin / configuration
  {
    to: "/admin/categories",
    label: "Categories & Fields",
    icon: IconCategory,
    testId: "nav-admin-categories",
    section: "Admin",
  },
  {
    to: "/admin/locations",
    label: "Locations",
    icon: IconMapPin,
    testId: "nav-admin-locations",
    section: "Admin",
  },
  // NOTE (M7, `docs/tasks/M7-project-grants.md`): the pre-M7 thin Admin CRUD
  // page (`/admin/projects`, `ProjectsScreen`/`ProjectFormModal` — name/lead/
  // is_active only) is deliberately DROPPED from nav here, superseded by the
  // top-level `/projects` hub above: that hub's own list screen now covers
  // the SAME create/delete (Admin-only `tenant.manage`, unchanged contract)
  // plus the full M7 grant surface, so keeping both nav entries would be two
  // competing "projects" experiences (CLAUDE.md/task brief). The route/
  // component are left in place (unregistered from nav only, see `App.tsx`)
  // rather than deleted, in case a direct link is bookmarked.
  {
    to: "/admin/users",
    label: "Users & Roles",
    icon: IconUsers,
    testId: "nav-admin-users",
    hidden: (me) => !hasUserManagePermission(me),
    section: "Admin",
  },
  {
    to: "/admin/email-settings",
    label: "Email Settings",
    icon: IconMail,
    testId: "nav-admin-email-settings",
    // Unlike Categories/Locations/Projects above (visible to everyone,
    // read-only for non-managers), the server gates the GET itself on
    // `tenant.manage` (`EmailSettingsScreen` doc comment) — there is no
    // read-only view for a non-admin, so hide the nav entry entirely.
    hidden: (me) => !hasPermission(me, TENANT_MANAGE),
    section: "Admin",
  },
];

/**
 * Mobile bottom tab bar (4-5 slots — a real tab bar can't fit ~10 sections).
 * Picked for what a lab tech does most, one-handed, day to day (`docs/
 * overview.md`'s user story): land on the dashboard, browse/find an asset,
 * scan a QR label (the app's signature action — promoted into the tab bar
 * itself, visually elevated, instead of a separate floating FAB now that a
 * real nav bar exists), and check what they personally have out. Everything
 * else (Stock, Reservations, Approvals, Notifications, Audit, Labels, Admin)
 * lives one tap away in the "More" sheet (`MOBILE_MORE_ITEMS`), so no route
 * is ever unreachable on a phone.
 */
export const MOBILE_TABS: NavItem[] = [
  { to: "/", label: "Home", icon: IconLayoutDashboard, testId: "tab-dashboard" },
  { to: "/assets", label: "Assets", icon: IconBox, testId: "tab-assets" },
  { to: "/scan", label: "Scan", icon: IconQrcode, testId: "scan-fab" },
  { to: "/my-items", label: "My Items", icon: IconUserCircle, testId: "tab-my-items" },
];

const MOBILE_TAB_PATHS = new Set(MOBILE_TABS.map((t) => t.to));

/** Every other nav item, for the mobile "More" drawer — computed from
 * `NAV_ITEMS` so it can never drift out of sync (a route added to
 * `NAV_ITEMS` automatically becomes reachable on mobile, either via a tab or
 * via this overflow list). */
export const MOBILE_MORE_ITEMS: NavItem[] = NAV_ITEMS.filter((item) => !MOBILE_TAB_PATHS.has(item.to));
