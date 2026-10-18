// ABOUTME: 界面语言上下文：持有语言与取词字典，挂在 Provider 链最外层（登录页也要取词）。
// ABOUTME(en): UI locale context holding the active locale and its dictionary; mounted outermost.
"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import type { Dictionary } from "./dictionary";
import { en } from "./en";
import { htmlLang, type UiLocale } from "./locale";
import {
  LOCALE_DATASET_ATTRIBUTE,
  readStoredLocale,
  writeStoredLocale,
  type StoredLocale,
  type LocaleSource,
} from "./locale-storage";
import { zhHans } from "./zh-Hans";

const DICTIONARIES: Record<UiLocale, Dictionary> = { "zh-Hans": zhHans, en };

interface LocaleContextValue {
  locale: UiLocale;
  source: LocaleSource;
  /** 用户显式选择：此后任何报告都不再改写界面语言。 */
  selectLocale: (locale: UiLocale) => void;
  /** 报告语言派生的默认值；用户显式选过后是 no-op。 */
  adoptDerived: (locale: UiLocale) => void;
}

const LocaleContext = createContext<LocaleContextValue>({
  locale: "zh-Hans",
  source: "derived",
  selectLocale: () => {},
  adoptDerived: () => {},
});

export function LocaleProvider({
  children,
  serverLocale,
}: {
  children: React.ReactNode;
  /** SSR 判定的语言（cookie → Accept-Language，见 locale-server.ts）。客户端初值必须与它一致，否则取词树水合不匹配。 */
  serverLocale: UiLocale;
}) {
  // 初值来自服务端判定，与 SSR 输出的那帧严格一致；挂载后再按本机显式选择校正。
  const [state, setState] = useState<StoredLocale>(() => ({
    locale: serverLocale,
    source: "derived",
  }));

  useEffect(() => {
    const stored = readStoredLocale();
    if (!stored || stored.source !== "explicit" || stored.locale === state.locale) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 挂载后校正本机显式选择；初值必须来自 SSR，不能在渲染期读 localStorage
    setState(stored);
  }, [state.locale]);

  useEffect(() => {
    document.documentElement.setAttribute(LOCALE_DATASET_ATTRIBUTE, state.locale);
    document.documentElement.lang = htmlLang(state.locale);
  }, [state.locale]);

  const selectLocale = useCallback((locale: UiLocale) => {
    const next = { locale, source: "explicit" as const };
    setState(next);
    writeStoredLocale(next);
  }, []);

  const adoptDerived = useCallback((locale: UiLocale) => {
    setState((current) => {
      if (current.source === "explicit" || current.locale === locale) return current;
      const next = { locale, source: "derived" as const };
      writeStoredLocale(next);
      return next;
    });
  }, []);

  const value = useMemo<LocaleContextValue>(
    () => ({ locale: state.locale, source: state.source, selectLocale, adoptDerived }),
    [state.locale, state.source, selectLocale, adoptDerived],
  );

  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>;
}

export function useLocale(): LocaleContextValue {
  return useContext(LocaleContext);
}

/** 取词：返回整棵字典，按命名空间属性访问（拼错 key 即编译期失败）。 */
export function useT(): Dictionary {
  return DICTIONARIES[useContext(LocaleContext).locale];
}
