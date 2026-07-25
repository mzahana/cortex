import {
  IconBell,
  IconBox,
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
  IconSitemap,
  IconUserCircle,
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
}

/**
 * The full set of top-level destinations (~10 sections), in the order the
 * desktop sidebar renders them. Every route in `App.tsx` is reachable from
 * this list — the mobile bottom-tab bar (`MOBILE_TABS` below) only ever
 * surfaces a subset, with the rest reachable through the "More" sheet, so
 * nothing here becomes unreachable on a phone.
 */
export const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Dashboard", icon: IconLayoutDashboard, testId: "nav-dashboard" },
  { to: "/assets", label: "Assets", icon: IconBox, testId: "nav-assets" },
  { to: "/scan", label: "Scan", icon: IconQrcode, testId: "nav-scan" },
  { to: "/stock", label: "Stock", icon: IconPackages, testId: "nav-stock" },
  { to: "/reservations", label: "Reservations", icon: IconCalendarEvent, testId: "nav-reservations" },
  { to: "/approvals", label: "Approvals", icon: IconClipboardList, testId: "nav-approvals" },
  { to: "/my-items", label: "My Items", icon: IconUserCircle, testId: "nav-my-items" },
  { to: "/notifications", label: "Notifications", icon: IconBell, testId: "nav-notifications" },
  {
    to: "/labels",
    label: "Print Labels",
    icon: IconPrinter,
    testId: "nav-labels",
    hidden: (me) => !hasAnyAssetPermission(me, LABEL_GENERATE),
  },
  {
    to: "/audit",
    label: "Audit Log",
    icon: IconHistory,
    testId: "nav-audit",
    hidden: (me) => !hasAuditViewPermission(me),
  },
  {
    to: "/import",
    label: "Bulk Import",
    icon: IconFileImport,
    testId: "nav-import",
    hidden: (me) => !hasImportRunPermission(me),
  },
  { to: "/admin/categories", label: "Categories & Fields", icon: IconCategory, testId: "nav-admin-categories" },
  { to: "/admin/locations", label: "Locations", icon: IconMapPin, testId: "nav-admin-locations" },
  { to: "/admin/projects", label: "Projects", icon: IconSitemap, testId: "nav-admin-projects" },
  {
    to: "/admin/users",
    label: "Users & Roles",
    icon: IconUsers,
    testId: "nav-admin-users",
    hidden: (me) => !hasUserManagePermission(me),
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
