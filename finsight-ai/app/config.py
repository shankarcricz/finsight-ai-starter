from pathlib import Path
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / '.env', extra='ignore')
    openai_api_key: str
    pinecone_api_key: str
    pinecone_index: str = 'finsight-documents'
    openai_model: str = 'gpt-4.1-mini'
    embedding_model: str = 'text-embedding-3-small'
    demo_user_id: str = 'demo-user'
    # Absolute path so the DB location does not depend on the shell's working directory.
    database_url: str = f"sqlite:///{(PROJECT_ROOT / 'data' / 'finsight.sqlite3').as_posix()}"
    # Send only transaction descriptions that deterministic rules could not classify to the LLM.
    llm_categorization: bool = True
    # Firebase Authentication. Empty project id = local dev mode (single demo user, no login).
    firebase_project_id: str = ''
    firebase_web_api_key: str = ''   # public web config value, not a secret
    firebase_auth_domain: str = ''
    # Sign-in is open to any Google account. Admins (comma-separated emails) may also edit shared market data.
    admin_emails: str = ''

    @property
    def auth_enabled(self) -> bool:
        return bool(self.firebase_project_id)

    @property
    def admin_email_set(self) -> frozenset[str]:
        return frozenset(e.strip().lower() for e in self.admin_emails.split(',') if e.strip())

    # LangSmith tracing: on when an API key is present (LANGSMITH_TRACING=false turns it off).
    langsmith_api_key: str = Field('', validation_alias=AliasChoices('LANGSMITH_API_KEY', 'LANGSMITH_KEY'))
    langsmith_tracing: bool = True
    langsmith_project: str = 'finsight'
    langsmith_endpoint: str = ''          # EU workspaces: https://eu.api.smith.langchain.com
    langsmith_trace_reads: bool = True    # false = trace only actions, AI flows and jobs (saves trace quota)
    app_env: str = 'development'          # tag on every trace, e.g. production
    smtp_host: str = ''
    smtp_port: int = 587
    smtp_username: str = ''
    smtp_password: str = ''
    smtp_from: str = ''
    alert_to: str = ''
    enable_scheduler: bool = False
    review_day: str = 'sat'   # weekly portfolio review (Asia/Kolkata)
    review_hour: int = 9

settings = Settings()
