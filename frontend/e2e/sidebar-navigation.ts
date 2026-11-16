import type { Page } from "playwright-core";

/** The same sidebar owns primary destinations in docked and drawer layouts. */
export async function sidebarNavigation(page: Page) {
  // A resize delivers matchMedia changes during rendering. Let that frame commit before
  // deciding whether navigation is docked or needs its drawer.
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
  const toggle = page.getByRole("button", { name: "Sessions and agents", exact: true });
  if ((await toggle.isVisible()) && (await toggle.getAttribute("aria-expanded")) !== "true") {
    await toggle.click();
  }
  const navigation = page.getByRole("navigation", { name: "Navigation and sessions" });
  await navigation.waitFor();
  await navigation.evaluate(async (element) => {
    const drawer = element.closest(".rail-drawer");
    if (!drawer) return;
    await Promise.all(drawer.getAnimations().map((animation) => animation.finished));
  });
  return navigation;
}

export async function navigateSidebar(page: Page, view: string) {
  if (view === "Code") {
    const workbench = page.locator(".workbench");
    const drawer = page.getByRole("dialog", { name: "Sessions and agents", exact: true });
    if (await workbench.isVisible()) {
      if (await drawer.isVisible())
        await drawer.getByRole("button", { name: "Close", exact: true }).click();
      return;
    }
    const navigation = await sidebarNavigation(page);
    await navigation.getByRole("button", { name: "Chat", exact: true }).click();
    const toggle = page.getByRole("button", { name: "VIKI panel", exact: true });
    if ((await toggle.isVisible()) && (await toggle.getAttribute("aria-expanded")) !== "true")
      await toggle.click();
    await page
      .getByRole("tablist", { name: "Right sidebar", exact: true })
      .getByRole("tab", { name: "Code", exact: true })
      .click();
    await page
      .getByRole("region", { name: "Code sidebar", exact: true })
      .getByRole("button", { name: "Open workbench", exact: true })
      .click();
    await workbench.waitFor();
    return;
  }
  const navigation = await sidebarNavigation(page);
  await navigation.getByRole("button", { name: new RegExp(`^${view}(?:[\\s,]|$)`) }).click();
}

export async function newSidebarSession(page: Page) {
  const navigation = await sidebarNavigation(page);
  await navigation.getByRole("button", { name: "New session", exact: true }).click();
}

/** A visible shell control outside the conversation, without opening a modal drawer. */
export async function focusShellControl(page: Page) {
  const session = page.getByRole("button", { name: "New session", exact: true });
  if (await session.isVisible()) await session.focus();
  else await page.getByRole("button", { name: "Sessions and agents", exact: true }).focus();
}
