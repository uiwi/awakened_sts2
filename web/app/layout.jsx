import "./globals.css";

export const metadata = {
  title: "Spire Archive — 슬레이 더 스파이어 2 카드 검색과 평가",
  description: "슬레이 더 스파이어 2의 전체 카드 577장을 검색하고 가치 모델로 비교합니다.",
};

export default function RootLayout({ children }) {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
