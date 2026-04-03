# USD/KRW Quant Bot — Railway 배포 가이드

## 📁 파일 구성
```
railway_bot/
├── main.py           ← 봇 메인 코드
├── requirements.txt  ← 패키지 목록
├── Procfile          ← Railway 실행 명령
└── README.md         ← 이 파일
```

---

## 🚀 배포 순서

### STEP 1. Google 서비스 계정 만들기
1. https://console.cloud.google.com 접속
2. 새 프로젝트 생성 (이름 자유)
3. 상단 검색창 → "Google Sheets API" 검색 → 사용 설정
4. 상단 검색창 → "Google Drive API" 검색 → 사용 설정
5. 왼쪽 메뉴 → IAM 및 관리자 → 서비스 계정
6. "+ 서비스 계정 만들기" 클릭
7. 이름 입력 후 생성 (역할은 건너뛰기)
8. 생성된 서비스 계정 클릭 → 키 탭 → 키 추가 → JSON
9. 다운로드된 JSON 파일 내용 전체 복사해두기

### STEP 2. Google Sheets 공유 설정
1. Google Sheets 에서 "USDKRW_Quant_v5" 스프레드시트 열기
   (없으면 봇 첫 실행 시 자동 생성됨)
2. 공유 버튼 클릭
3. 서비스 계정 이메일 주소 입력 (JSON 파일 안의 client_email 값)
4. 편집자 권한으로 공유

### STEP 3. GitHub 저장소 만들기
1. https://github.com/new 접속
2. 저장소 이름 입력 (예: usdkrw-quant-bot)
3. Private으로 생성
4. main.py / requirements.txt / Procfile 업로드

### STEP 4. Railway 배포
1. https://railway.com/new 접속
2. "Deploy from GitHub repo" 선택
3. 위에서 만든 저장소 선택
4. 배포 시작 (자동으로 requirements.txt 인식)

### STEP 5. 환경변수 설정 (중요!)
Railway 대시보드 → 프로젝트 → Variables 탭에서 아래 4개 추가:

| 변수명 | 값 |
|--------|-----|
| TELEGRAM_TOKEN | 텔레그램 봇 토큰 |
| TELEGRAM_CHAT_ID | 텔레그램 채팅 ID |
| SHEET_NAME | USDKRW_Quant_v5 |
| GOOGLE_CREDENTIALS_JSON | 서비스 계정 JSON 전체 (한 줄로) |

※ GOOGLE_CREDENTIALS_JSON 입력 시:
   JSON 파일을 열어서 내용 전체를 복사 → 그대로 붙여넣기

### STEP 6. 실행 확인
1. Railway → Deployments 탭에서 로그 확인
2. "✅ 봇 시작 완료" 로그가 보이면 성공
3. 텔레그램에 "🤖 봇 시작!" 메시지가 오면 완료

---

## ✅ 완료 후 동작
- 24시간 자동 실행 (Colab 불필요)
- 매일 09:00 KST 일간 리포트 자동 발송
- /long /short /exit /status 명령어 실시간 응답
- 스톱로스 근접·청산 추천 알림 자동 발송
