"""Initial Tier 2 schema

Revision ID: 001
Revises:
Create Date: 2026-07-31
"""
from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── reminder_records ──────────────────────────────────────────────────────
    op.create_table(
        "reminder_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("emr_appt_id", sa.String(128), nullable=False),
        sa.Column("emr_patient_id", sa.String(128), nullable=False),
        sa.Column("appt_datetime", sa.DateTime(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "sent", "confirmed", "declined",
                "no_response", "failed",
                name="reminderstatus",
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("notifyre_message_id", sa.String(256)),
        sa.Column("sent_at", sa.DateTime()),
        sa.Column("replied_at", sa.DateTime()),
        sa.Column("reply_text", sa.Text()),
        sa.Column("emr_writeback_done", sa.Boolean(), server_default="false"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_reminder_tenant", "reminder_records", ["tenant_id"])
    op.create_index("ix_reminder_appt", "reminder_records", ["emr_appt_id"])

    # ── rebook_prompts ────────────────────────────────────────────────────────
    op.create_table(
        "rebook_prompts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("reminder_id", sa.Integer(), sa.ForeignKey("reminder_records.id")),
        sa.Column("emr_appt_id", sa.String(128), nullable=False),
        sa.Column("notifyre_message_id", sa.String(256)),
        sa.Column("sent_at", sa.DateTime()),
        sa.Column("status", sa.String(32), server_default="sent"),
    )

    # ── eligibility_checks ────────────────────────────────────────────────────
    op.create_table(
        "eligibility_checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("emr_appt_id", sa.String(128), nullable=False),
        sa.Column("emr_patient_id", sa.String(128), nullable=False),
        sa.Column("appt_date", sa.Date(), nullable=False),
        sa.Column("payer_id", sa.String(64), nullable=False),
        sa.Column("payer_name", sa.String(256), server_default=""),
        sa.Column("member_id", sa.String(128)),
        sa.Column("group_number", sa.String(128)),
        sa.Column("plan_name", sa.String(256)),
        sa.Column("office_visit_copay", sa.String(32)),
        sa.Column("coverage_begin", sa.Date()),
        sa.Column("coverage_end", sa.Date()),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "active", "inactive", "manual", "error",
                name="eligibilitycheckstatus",
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("is_priority_payer", sa.Boolean(), server_default="false"),
        sa.Column("office_notified", sa.Boolean(), server_default="false"),
        sa.Column("raw_response", sa.JSON()),
        sa.Column("checked_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_elig_tenant", "eligibility_checks", ["tenant_id"])
    op.create_index("ix_elig_appt", "eligibility_checks", ["emr_appt_id"])

    # ── fax_routing_rules ─────────────────────────────────────────────────────
    op.create_table(
        "fax_routing_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("from_number", sa.String(32)),
        sa.Column("from_number_prefix", sa.String(16)),
        sa.Column("sender_org_name", sa.String(256)),
        sa.Column("queue", sa.String(64), nullable=False),
        sa.Column("is_catch_all", sa.Boolean(), server_default="false"),
        sa.Column("priority", sa.Integer(), server_default="100"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_fax_rule_tenant", "fax_routing_rules", ["tenant_id"])

    # ── fax_records ───────────────────────────────────────────────────────────
    op.create_table(
        "fax_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column(
            "direction",
            sa.Enum("inbound", "outbound", name="faxdirection"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "received", "routed", "unrouted", "queued", "sent", "failed",
                name="faxqueuestatus",
            ),
            nullable=False,
        ),
        sa.Column("external_fax_id", sa.String(256)),
        sa.Column("from_number", sa.String(32)),
        sa.Column("to_number", sa.String(32)),
        sa.Column("pages", sa.Integer()),
        sa.Column("routed_to_queue", sa.String(64)),
        sa.Column("routing_rule_id", sa.Integer()),
        sa.Column("emr_patient_id", sa.String(128)),
        sa.Column("subject", sa.String(256)),
        sa.Column("receiving_org", sa.String(256)),
        sa.Column("delivery_confirmed_at", sa.DateTime()),
        sa.Column("emr_logged", sa.Boolean(), server_default="false"),
        sa.Column("notes", sa.Text()),
        sa.Column("received_at", sa.DateTime()),
        sa.Column("sent_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_fax_record_tenant", "fax_records", ["tenant_id"])

    # ── care_gap_measures ─────────────────────────────────────────────────────
    op.create_table(
        "care_gap_measures",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("sms_label", sa.String(128), nullable=False),
        sa.Column("interval_months", sa.Integer()),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # ── care_gap_records ──────────────────────────────────────────────────────
    op.create_table(
        "care_gap_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("emr_patient_id", sa.String(128), nullable=False),
        sa.Column("measure_code", sa.String(32), nullable=False),
        sa.Column(
            "source",
            sa.Enum("emr_due_date", "payer_list", name="caregapsource"),
            nullable=False,
        ),
        sa.Column("payer_list_upload_id", sa.Integer()),
        sa.Column("payer_id", sa.String(64)),
        sa.Column("due_date", sa.Date()),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "sent", "responded", "scheduled", "suppressed", "failed",
                name="outreachstatus",
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("notifyre_message_id", sa.String(256)),
        sa.Column("sent_at", sa.DateTime()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_care_gap_tenant", "care_gap_records", ["tenant_id"])
    op.create_index("ix_care_gap_patient", "care_gap_records", ["emr_patient_id"])

    # ── payer_list_uploads ────────────────────────────────────────────────────
    op.create_table(
        "payer_list_uploads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("payer_id", sa.String(64), nullable=False),
        sa.Column("payer_name", sa.String(256), server_default=""),
        sa.Column("measure_code", sa.String(32), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("row_count", sa.Integer(), server_default="0"),
        sa.Column("outreach_sent", sa.Integer(), server_default="0"),
        sa.Column("status", sa.String(32), server_default="pending"),
        sa.Column("uploaded_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("processed_at", sa.DateTime()),
    )

    # ── Seed default care gap measures (pilot clinic) ─────────────────────────
    op.execute("""
        INSERT INTO care_gap_measures (tenant_id, code, display_name, sms_label, interval_months, is_active)
        VALUES
          ('pilot-clinic-1', 'AWV',       'Annual Wellness Visit',          'Annual Wellness Visit',       12, true),
          ('pilot-clinic-1', 'TCM',        'Transitional Care Management',  'follow-up visit',              2, true),
          ('pilot-clinic-1', 'BRCA',       'Breast Cancer Screening',       'mammogram',                   12, true),
          ('pilot-clinic-1', 'CORC',       'Colorectal Cancer Screening',   'colorectal cancer screening', 12, true),
          ('pilot-clinic-1', 'DM_A1C',     'Diabetes A1c',                  'A1c lab check',                3, true),
          ('pilot-clinic-1', 'DM_EYE',     'Diabetes Eye Exam',             'diabetic eye exam',           12, true),
          ('pilot-clinic-1', 'BP_FOLLOW',  'Blood Pressure Follow-up',      'blood pressure follow-up',     3, true)
    """)


def downgrade() -> None:
    op.drop_table("payer_list_uploads")
    op.drop_table("care_gap_records")
    op.drop_table("care_gap_measures")
    op.drop_table("fax_records")
    op.drop_table("fax_routing_rules")
    op.drop_table("eligibility_checks")
    op.drop_table("rebook_prompts")
    op.drop_table("reminder_records")

    for enum_name in (
        "reminderstatus", "eligibilitycheckstatus",
        "faxdirection", "faxqueuestatus",
        "caregapsource", "outreachstatus",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
