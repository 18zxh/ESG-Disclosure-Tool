// ABOUTME: 服务端语言判定——cookie 优先，其次 Accept-Language；与首帧脚本同序，消除水合分叉。
// ABOUTME(en): Server-side locale decision — cookie first, then Accept-Language; mirrors the head script.
import { cookies, headers } from "next/headers";

import { isUiLocale, type UiLocale } from "./locale";
import { LOCALE_COOKIE_NAME } from "./locale-storage";

/**
 * SSR 的界面语言判定，解析顺序与 `LOCALE_BOOTSTRAP_SCRIPT` 保持同源：
 *
 * 1. cookie：用户或首帧脚本写下的偏好（`writeLocaleCookie` 双写）；
 * 2. Accept-Language：首次到访、还没有任何偏好时，按浏览器语言给出与服务端
 *    默认一致的结果——服务端此前恒为 en，中文浏览器首访必然水合不匹配。
 *
 * Provider 的 initial state 直接消费本函数结果，保证 SSR 与客户端首帧严格一致；
 * 浏览器里的 localStorage 只在挂载后用于校正（explicit 优先），不再参与初值。
 */
export async function resolveServerLocale(): Promise<UiLocale> {
  const cookieStore = await cookies();
  const cookieLocale = cookieStore.get(LOCALE_COOKIE_NAME)?.value;
  if (isUiLocale(cookieLocale)) return cookieLocale;

  const headerStore = await headers();
  const acceptLanguage = (headerStore.get("accept-language") ?? "").toLowerCase();
  return acceptLanguage.startsWith("zh") ? "zh-Hans" : "en";
}
