// ABOUTME: 界面语言偏好的本机存储边界：localStorage（设备明细）+ cookie（SSR 可读）+ 首帧脚本。
// ABOUTME(en): Device-local UI locale preference: localStorage for detail, cookie for SSR, head script for first paint.
import { isUiLocale, type UiLocale } from "./locale";

/** 与 AppShell 的 nav-collapsed 同一命名惯例。不按 accountId 分片：界面语言是设备偏好。 */
export const LOCALE_STORAGE_KEY = "sustainability-desk:ui-locale";

/** 与 LOCALE_STORAGE_KEY 同值双写：cookie 让服务端在 SSR 时读到同一语言，消除水合分叉。 */
export const LOCALE_COOKIE_NAME = "sustainability-desk:ui-locale";

const LOCALE_COOKIE_MAX_AGE = 60 * 60 * 24 * 365;

/** 首帧脚本写在根元素上的属性；Provider 挂载后据此校正文档属性。 */
export const LOCALE_DATASET_ATTRIBUTE = "data-ui-locale";

/**
 * 语言来源。
 *
 * `explicit` = 用户在界面上选过：此后任何报告都不再改写它，界面语言是用户属性。
 * `derived` = 尚未选过：跟随当前报告所属知识包的语言。
 */
export type LocaleSource = "explicit" | "derived";

export interface StoredLocale {
  locale: UiLocale;
  source: LocaleSource;
}

export function readStoredLocale(): StoredLocale | null {
  try {
    const raw = window.localStorage.getItem(LOCALE_STORAGE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return null;
    const { locale, source } = parsed as { locale?: unknown; source?: unknown };
    if (!isUiLocale(locale)) return null;
    return { locale, source: source === "explicit" ? "explicit" : "derived" };
  } catch {
    // 隐私模式、禁用站点数据或损坏的值：按「未选过」处理，不阻断渲染。
    return null;
  }
}

export function writeStoredLocale(value: StoredLocale): void {
  try {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, JSON.stringify(value));
  } catch {
    // 存不下只影响下次访问的默认值，不影响本次会话。
  }
  writeLocaleCookie(value.locale);
}

/** 同步写 cookie：SSR 的语言判定只认得到 cookie，localStorage 它读不到。 */
export function writeLocaleCookie(locale: UiLocale): void {
  try {
    document.cookie = `${LOCALE_COOKIE_NAME}=${locale}; path=/; max-age=${LOCALE_COOKIE_MAX_AGE}; samesite=lax`;
  } catch {
    // cookie 写不进只影响下次 SSR 的语言判定，本次会话不受影响。
  }
}

/**
 * 首帧内联脚本：在 hydration 之前把界面语言写到根元素与 cookie。
 *
 * `app/layout.tsx` 是 Server Component，读不到 localStorage；首帧脚本保证
 * 「已画出的那一帧」用的是用户偏好，cookie 则让下一次 SSR 也用同一偏好。
 *
 * 内容是编译期常量、无用户输入。解析顺序必须与服务端 `resolveServerLocale`
 * （cookie → Accept-Language）一致——两处分叉会让首帧与 SSR 输出互不相同，
 * 反而制造闪烁与水合不匹配。
 */
export const LOCALE_BOOTSTRAP_SCRIPT = `(function(){try{
var raw=localStorage.getItem(${JSON.stringify(LOCALE_STORAGE_KEY)});
var locale=null;
if(raw){var v=JSON.parse(raw);if(v&&(v.locale==="en"||v.locale==="zh-Hans"))locale=v.locale;}
if(!locale)locale=(navigator.language||"").toLowerCase().indexOf("zh")===0?"zh-Hans":"en";
document.documentElement.setAttribute(${JSON.stringify(LOCALE_DATASET_ATTRIBUTE)},locale);
document.documentElement.lang=locale==="en"?"en-HK":"zh-CN";
document.cookie=${JSON.stringify(LOCALE_COOKIE_NAME)}+"="+locale+"; path=/; max-age=${LOCALE_COOKIE_MAX_AGE}; samesite=lax";
}catch(e){}})()`;
