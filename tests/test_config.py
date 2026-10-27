from mcp_email_server.config import Settings


def test_settings_exposes_single_default_account() -> None:
    settings = Settings(
        _env_file=None,
        imap_host="mail.test.local",
        imap_username="agent@test.local",
        imap_password="secret",
        smtp_host="mail.test.local",
        smtp_username="agent@test.local",
        smtp_password="secret",
        smtp_from_address="agent@test.local",
    )

    accounts = settings.account_configs()
    resolved = settings.for_account("agent")

    assert len(accounts) == 1
    assert accounts[0].account_name == "agent"
    assert accounts[0].resolved_email_address() == "agent@test.local"
    assert resolved.imap_username == "agent@test.local"
    assert resolved.resolved_mailbox_id() == "agent"


def test_settings_resolves_accounts_from_accounts_json() -> None:
    settings = Settings(
        _env_file=None,
        imap_host="mail.shared.local",
        smtp_host="mail.shared.local",
        openrouter_api_key="router-secret",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-secret",
        accounts_json='['
        '{"account_name":"sales","imap_username":"sales@example.com","imap_password":"sales-pass","smtp_username":"sales@example.com","smtp_password":"sales-pass","smtp_from_address":"sales@example.com"},'
        '{"account_name":"support","mailbox_id":"support-box","imap_username":"support@example.com","imap_password":"support-pass","smtp_username":"support@example.com","smtp_password":"support-pass","smtp_from_address":"support@example.com","imap_mailbox":"Support"}'
        ']',
    )

    accounts = settings.account_configs()
    support = settings.for_account("support")
    support_by_email = settings.for_account("support@example.com")

    assert [account.account_name for account in accounts] == ["sales", "support"]
    assert settings.default_account_name() == "sales"
    assert support.imap_host == "mail.shared.local"
    assert support.imap_username == "support@example.com"
    assert support.imap_password.get_secret_value() == "support-pass"
    assert support.smtp_username == "support@example.com"
    assert support.resolved_mailbox_id() == "support-box"
    assert support.imap_mailbox == "Support"
    assert support_by_email.smtp_from_address == "support@example.com"


def test_settings_propagate_persona_fields_per_account() -> None:
    settings = Settings(
        _env_file=None,
        imap_host="mail.shared.local",
        smtp_host="mail.shared.local",
        accounts_json='['
        '{"account_name":"sales","imap_username":"sales@example.com","imap_password":"sales-pass","persona_name":"Sales closer","persona_instructions":"Be concise and commercial.","signature":"Sales Team"},'
        '{"account_name":"support","imap_username":"support@example.com","imap_password":"support-pass","persona_name":"Support advisor","persona_instructions":"Be calm and solution-oriented.","signature":"Support Desk"}'
        ']',
    )

    support = settings.for_account("support")

    assert support.persona_name == "Support advisor"
    assert support.persona_instructions == "Be calm and solution-oriented."
    assert support.signature == "Support Desk"


def test_settings_expose_required_hubspot_bcc_by_default_and_per_account() -> None:
    settings = Settings(
        _env_file=None,
        imap_host="mail.shared.local",
        smtp_host="mail.shared.local",
        accounts_json='['
        '{"account_name":"sales","imap_username":"sales@example.com","imap_password":"sales-pass","smtp_username":"sales@example.com","smtp_password":"sales-pass","smtp_from_address":"sales@example.com"},'
        '{"account_name":"support","imap_username":"support@example.com","imap_password":"support-pass","smtp_username":"support@example.com","smtp_password":"support-pass","smtp_from_address":"support@example.com","hubspot_bcc_address":"support-bcc@bcc.hubspot.com"}'
        ']',
    )

    sales = settings.for_account("sales")
    support = settings.for_account("support")

    assert sales.required_bcc_recipients() == ["4452112@bcc.hubspot.com"]
    assert support.required_bcc_recipients() == ["support-bcc@bcc.hubspot.com"]