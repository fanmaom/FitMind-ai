-- Docker 开发环境的最小权限角色。
-- fitness_admin 只负责初始化；API 使用受 FORCE RLS 约束的 fitness_app，
-- 后台任务领取器使用可跨租户扫描 jobs 的 fitness_worker。
CREATE EXTENSION IF NOT EXISTS vector;

CREATE ROLE fitness_app LOGIN PASSWORD 'fitness';
ALTER DATABASE fitness OWNER TO fitness_app;
ALTER SCHEMA public OWNER TO fitness_app;
GRANT CONNECT ON DATABASE fitness TO fitness_app;
GRANT USAGE, CREATE ON SCHEMA public TO fitness_app;

CREATE ROLE fitness_worker LOGIN PASSWORD 'fitness_worker' BYPASSRLS;
GRANT CONNECT ON DATABASE fitness TO fitness_worker;
GRANT USAGE ON SCHEMA public TO fitness_worker;

ALTER DEFAULT PRIVILEGES FOR ROLE fitness_app IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO fitness_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE fitness_app IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO fitness_worker;
