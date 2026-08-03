# awakened_sts2

현재 안정 버전의 **Slay the Spire 2 카드 전체 데이터셋**을 수집하는 프로젝트입니다.

웹 카드 검색기: <https://uiwi.github.io/awakened_sts2/>

## 수집

```bash
cd ~/awakened_sts2
python3 scripts/collect_cards.py
```

이미 존재하는 이미지는 SHA-256 검증만 하고 다시 받지 않습니다. 모두 새로 받으려면:

```bash
python3 scripts/collect_cards.py --refresh-images
```

## 결과

- `data/raw/`: Spire Codex API의 한국어·영어 원본 응답
- `data/processed/cards.json`: 모든 카드의 완전한 병합 데이터
- `data/processed/cards.csv`: Excel에서도 열 수 있는 UTF-8 BOM CSV
- `data/processed/keyword_index.json`: 카드 키워드 및 게임 용어 역색인
- `data/processed/keyword_index.csv`: 키워드 역색인 CSV
- `data/processed/image_manifest.json`: 이미지 출처·경로·크기·SHA-256
- `data/processed/validation_report.json`: 중복·누락·다운로드 검증
- `images/art/`: 카드 원본 아트
- `images/art_variants/`: 조합형 카드의 변형 아트
- `images/cards_ko/`: 한국어 일반 카드 전체 렌더
- `images/cards_ko_upgraded/`: 한국어 강화 카드 전체 렌더

카드별 주요 필드는 ID, 한·영 이름과 텍스트, 직업/풀, 카드 유형,
희귀도, 비용, 수치, 키워드, 강화 정보, 이미지 경로입니다.

## 출처 및 이용

- 데이터/API: [Spire Codex](https://spire-codex.com/)
- 추출기 소스: [ptrlrd/spire-codex](https://github.com/ptrlrd/spire-codex)
- 교차 검증: [Slay the Spire 2 Wiki](https://slaythespire.wiki.gg/wiki/Slay_the_Spire_2:Cards)

게임 데이터와 이미지는 Mega Crit Games의 저작물입니다. 이 결과물은 개인적
조사·참조 목적으로 사용하고 게임을 재컴파일하거나 재배포하는 데 사용하지 마세요.

## 카드 검색 웹 UI

브라우저용 데이터를 갱신한 뒤 Next.js 앱을 실행합니다.

```bash
python3 scripts/build_web_data.py
cd web
npm install
npm run dev
```

웹 UI는 카드 이름, 키워드, 직업/카드 풀, 희귀도 필터와 카드 상세 보기를 제공합니다.
카드 가치 모델 v5를 설명하는 평가 기준 페이지와 직업·카드 종류별 정렬이 가능한
전체 카드 평가표도 포함합니다. 평가 데이터를 갱신하려면 다음 명령을 실행합니다.

```bash
python3 scripts/build_evaluation_web_data.py
```

현재 생성된 정적 빌드는 Node.js 없이 Python으로 바로 실행할 수 있습니다.

```bash
cd ~/awakened_sts2
python3 -m http.server 4173 --directory web/out
```

브라우저에서 `http://localhost:4173`을 여세요.
