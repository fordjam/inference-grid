import { test, expect, Page } from "@playwright/test";

test.describe("views console", () => {
  test("rename updates the row without a reload", async ({ page }) => {
    await mockViewsApi(page);
    await page.goto("/views");
    await page.getByTestId("view-row-gallery").dblclick();
    await page.keyboard.press("End");
    await page.keyboard.type("-sunset");
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("view-row-gallery-sunset")).toBeVisible();
  });
});

async function mockViewsApi(page: Page) {
  await page.route("/api/views", (route) => {
    if (route.request().method() === "PUT") {
      return route.fulfill({ status: 200, body: '{"renamed": true}' });
    }
    return route.fulfill({ status: 200, body: "[]" });
  });
}
