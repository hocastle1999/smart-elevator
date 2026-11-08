-- 로컬 개발용 DB(개발, 테스트)와 전용 사용자를 만든다. root 계정으로 한 번만 실행한다.
-- 실행 전에 '여기에_비밀번호' 부분을 바꾸고, 같은 값을 .env의 DB_PASSWORD에 넣는다.
-- (이 파일에 실제 비밀번호를 적은 채로 커밋하지 말 것)

CREATE DATABASE IF NOT EXISTS smart_elevator
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

-- 테스트는 이 DB를 매번 비우고 마이그레이션으로 다시 만든다 (이름이 _test로 끝나야 실행됨).
CREATE DATABASE IF NOT EXISTS smart_elevator_test
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

CREATE USER IF NOT EXISTS 'smart_elevator_app'@'localhost' IDENTIFIED BY '여기에_비밀번호';

-- 앱 계정에는 이 두 DB에 대한 권한만 준다. (root를 앱에서 쓰지 않는다)
GRANT ALL PRIVILEGES ON smart_elevator.* TO 'smart_elevator_app'@'localhost';
GRANT ALL PRIVILEGES ON smart_elevator_test.* TO 'smart_elevator_app'@'localhost';
FLUSH PRIVILEGES;
