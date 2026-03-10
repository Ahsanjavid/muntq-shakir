from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_TEMPERATURE: float = 0.0

    SAP_BASE_URL: str = ""
    SAP_AUTH_TYPE: str = "basic"
    SAP_USERNAME: str = ""
    SAP_PASSWORD: str = ""
    SAP_CLIENT: str = "500"
    SAP_OAUTH_TOKEN_URL: str = ""
    SAP_OAUTH_CLIENT_ID: str = ""
    SAP_OAUTH_CLIENT_SECRET: str = ""

    REDIS_URL: str = "redis://localhost:6382/0"

    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 5002
    DASHBOARD_BASE_URL: str = "http://localhost:8002"
    MCP_SERVER_URL: str = "http://localhost:3002"

    # API key for authenticating /chat and /session endpoints (empty = auth disabled / dev mode)
    API_SECRET_KEY: str = ""

    # CORS — comma-separated origins, or "*" for dev-only
    CORS_ORIGINS: str = "http://localhost:5002,http://localhost:8002"

    # TLS verification for SAP HTTP calls (disable only for dev with self-signed certs)
    SAP_VERIFY_TLS: bool = True

    DEV_TENANT_ID: str = "TENANT_001"
    DEV_USER_ID: str = "USER_ADMIN"
    DEV_USER_ROLE: str = "admin"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        # Allow shared .env files to include compose-only variables.
        "extra": "ignore",
    }


settings = Settings()
