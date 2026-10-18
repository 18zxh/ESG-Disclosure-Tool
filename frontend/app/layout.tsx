// ABOUTME: 根布局：挂 Locale → Auth → Account → App 四层 Provider，任何页面都在报告上下文之内取得会话与账户。
// ABOUTME: Provider 顺序是契约——账户依赖会话、报告上下文依赖账户，颠倒会让门禁在首屏误判；
// ABOUTME: Locale 在最外层是因为登录页也要取词，而它在账户上下文之外。
import type { Metadata } from "next";
import Script from "next/script";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

import { AppProvider } from "@/lib/app-context";
import { AccountProvider } from "@/lib/account-context";
import { AuthProvider } from "@/lib/auth-context";
import { LocaleProvider } from "@/lib/i18n/locale-context";
import { resolveServerLocale } from "@/lib/i18n/locale-server";
import { LOCALE_BOOTSTRAP_SCRIPT } from "@/lib/i18n/locale-storage";
import { PRODUCT_NAME } from "@/lib/product-name";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: PRODUCT_NAME,
  description: "Draft sustainability reports against exchange disclosure rules, then export to Word.",
};

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // SSR 与客户端必须对「界面语言」给出同一个答案，否则整棵取词树水合不匹配：
  // 服务端按 cookie → Accept-Language 判定（见 resolveServerLocale），首帧脚本
  // 用同一顺序写根元素属性与 cookie。next/script 的 beforeInteractive 是 Next 16
  // 对「组件内裸 <script>」警告的官方替代，内联脚本随初始 HTML 同步执行。
  const serverLocale = await resolveServerLocale();
  return (
    /* lang 由首帧脚本按用户偏好改写；suppressHydrationWarning 只覆盖这一个属性。 */
    <html
      lang="zh-CN"
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <head>
        <Script
          id="locale-bootstrap"
          strategy="beforeInteractive"
          dangerouslySetInnerHTML={{ __html: LOCALE_BOOTSTRAP_SCRIPT }}
        />
      </head>
      <body className="min-h-full flex flex-col">
        <LocaleProvider serverLocale={serverLocale}>
          <AuthProvider>
            <AccountProvider>
              <AppProvider>{children}</AppProvider>
            </AccountProvider>
          </AuthProvider>
        </LocaleProvider>
      </body>
    </html>
  );
}
