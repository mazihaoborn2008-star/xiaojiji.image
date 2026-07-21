from pydantic import BaseModel, Field


class UserSession(BaseModel):
    user_id: str
    username: str
    avatar: str | None = None
    provider: str = "discord"
    discord_user_id: str | None = None
    is_dev: bool = False


class PromptRefineRequest(BaseModel):
    text: str = Field(min_length=1, max_length=3000)


class PromptRefineResponse(BaseModel):
    prompt: str


class EmailCodeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class EmailVerifyRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    code: str = Field(min_length=6, max_length=6)
    invite_code: str | None = Field(default=None, max_length=32)


class ProfileUsernameRequest(BaseModel):
    display_username: str = Field(min_length=1, max_length=40)


class EmailPasswordLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class EmailPasswordSetRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128)
    confirm_password: str = Field(min_length=1, max_length=128)
    old_password: str | None = Field(default=None, max_length=128)
    revoke_other_sessions: bool = False


class EmailPasswordResetVerifyRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    code: str = Field(min_length=6, max_length=6)


class EmailPasswordResetCompleteRequest(BaseModel):
    reset_token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=1, max_length=128)
    confirm_password: str = Field(min_length=1, max_length=128)


class TopupCreateRequest(BaseModel):
    amount_rmb: str | None = Field(default=None, max_length=20)
    payment_method: str = Field(default="wechat_qr", max_length=32)
    credits: int | None = Field(default=None, ge=1, le=100000)


class FeedbackCreateRequest(BaseModel):
    category: str = Field(default="other", max_length=32)
    message: str = Field(min_length=1, max_length=1000)


class SupportThreadCreateRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=80)
    category: str = Field(default="general", max_length=32)
    subject: str | None = Field(default=None, max_length=160)
    message: str = Field(min_length=1, max_length=2000)
    related_feedback_id: int | None = None
    related_topup_code: str | None = Field(default=None, max_length=40)
    priority: str = Field(default="normal", max_length=32)


class SupportMessageCreateRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class SmartAgentTaskRequest(BaseModel):
    request: str = Field(min_length=1, max_length=1200)


class SmartAgentMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class SmartAgentGenerateRequest(BaseModel):
    message: str = Field(default="生成吧", max_length=2000)


class ImageRefundCreateRequest(BaseModel):
    job_code: str = Field(min_length=4, max_length=40)
    user_note: str | None = Field(default="", max_length=500)
    confirm_severe_only: bool = False


class ImageRefundManualReviewRequest(BaseModel):
    user_note: str | None = Field(default="", max_length=500)


class AdminImageRefundActionRequest(BaseModel):
    note: str | None = Field(default="", max_length=500)
