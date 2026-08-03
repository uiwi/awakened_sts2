"use client";

import Link from "next/link";

const ITEMS = [
  { key: "cards", href: "/", label: "카드 도감" },
  { key: "guide", href: "/evaluation-guide", label: "평가 기준" },
  { key: "table", href: "/evaluation-table", label: "카드 평가표" },
];

export default function SiteNav({ active }) {
  return (
    <nav className="site-nav" aria-label="주요 메뉴">
      <Link className="site-brand" href="/" aria-label="Spire Archive 홈">
        <span aria-hidden="true">Ⅱ</span>
        <strong>SPIRE ARCHIVE</strong>
      </Link>
      <div className="site-nav-links">
        {ITEMS.map((item) => (
          <Link
            key={item.key}
            href={item.href}
            className={active === item.key ? "active" : ""}
            aria-current={active === item.key ? "page" : undefined}
          >
            {item.label}
          </Link>
        ))}
      </div>
    </nav>
  );
}
