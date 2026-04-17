"""AI description generation using Foundation Model API."""

import json
import logging

from openai import OpenAI

from .config import get_oauth_token, get_workspace_host, app_config

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """You are a data catalog documentation expert. Generate clear, concise,
business-friendly descriptions for database tables and columns.

Rules:
- Table descriptions: 1-3 sentences explaining what the table stores, its business purpose,
  and key relationships.
- Column descriptions: 1 sentence explaining what the column represents in business terms.
- Use plain English, avoid jargon where possible.
- Be specific about data types, units, and formats when relevant.
- Do not include the column name or type in the description — the reader already sees those.
- Return valid JSON only, no markdown fences."""

def _build_system_prompt(rules_override: str | None = None) -> str:
    """Build system prompt including Responsible AI rules.

    Args:
        rules_override: If provided, replaces the org rules from config for this
                        generation only (per-session override). Pass None to use
                        the org rules from config.yaml.
    """
    prompt = DEFAULT_SYSTEM_PROMPT
    rules = rules_override if rules_override is not None else app_config.responsible_ai_rules
    if rules:
        prompt += f"\n\nAdditional organizational rules:\n{rules}"
    return prompt


def _get_client() -> OpenAI:
    host = get_workspace_host()
    token = get_oauth_token()
    return OpenAI(api_key=token, base_url=f"{host}/serving-endpoints")


def generate_descriptions(
    table_info: dict,
    model: str | None = None,
    rules_override: str | None = None,
    reference_context: list[dict] | None = None,
) -> dict:
    """Generate AI descriptions for a table and all its columns.

    Parameters
    ----------
    reference_context: Optional list of ``{"text", "source", "score"}`` chunks
        retrieved from the reference-docs BM25 index. When provided, the chunks
        are spliced into the user prompt so the model can use them to inform
        descriptions. The sources are echoed back in the returned dict under
        the ``sources`` key.

    Returns:
        {
            "table_description": "...",
            "column_descriptions": {"col_name": "suggested description", ...},
            "sources": [{"source", "snippet"}, ...]  # only when reference_context provided
        }
    """
    model = model or app_config.serving_endpoint
    client = _get_client()

    # Build context about the table
    col_info = "\n".join(
        f"  - {c['name']} ({c['type_text']}){' — current: ' + c['comment'] if c['comment'] else ''}"
        for c in table_info["columns"]
    )

    context_block = ""
    if reference_context:
        parts = []
        for ref in reference_context:
            source = ref.get("source", "unknown")
            text = (ref.get("text") or "").strip()
            if text:
                parts.append(f"[Source: {source}]\n{text}")
        if parts:
            context_block = (
                "Reference documentation (use to inform descriptions; ignore irrelevant parts):\n"
                "---\n"
                + "\n---\n".join(parts)
                + "\n---\n\n"
            )

    user_prompt = f"""{context_block}Generate descriptions for this Unity Catalog table and its columns.

Table: {table_info['full_name']}
Type: {table_info['table_type']}
Format: {table_info.get('data_source_format', 'N/A')}
Current table description: {table_info['comment'] or '(none)'}

Columns:
{col_info}

Return JSON in this exact format:
{{
  "table_description": "description of the table",
  "column_descriptions": {{
    "column_name": "description",
    ...
  }}
}}"""

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _build_system_prompt(rules_override=rules_override)},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=4096,
        temperature=0.3,
    )

    content = response.choices[0].message.content.strip()
    # Strip markdown fences if present
    if content.startswith("```"):
        content = content.split("\n", 1)[1]
        if content.endswith("```"):
            content = content[:-3].strip()

    result = json.loads(content)
    if reference_context:
        result["sources"] = [
            {"source": r.get("source", ""), "snippet": (r.get("text") or "")[:200]}
            for r in reference_context
        ]
    return result


def generate_notebook_code(
    catalog_name: str,
    schema_name: str,
) -> str:
    """Generate a downloadable Databricks notebook that automates AI description generation."""
    rules_block = ""
    if app_config.responsible_ai_rules:
        rules_block = f'''
CUSTOM_RULES = """{app_config.responsible_ai_rules}"""
system_prompt += f"\\n\\nAdditional organizational rules:\\n{{CUSTOM_RULES}}"
'''

    notebook = f'''# Databricks notebook source
# MAGIC %md
# MAGIC # AI-Powered Table & Column Descriptions
# MAGIC
# MAGIC **Target Schema:** `{catalog_name}.{schema_name}`
# MAGIC
# MAGIC This notebook generates AI descriptions for all tables and columns in the target schema,
# MAGIC writes them to a review table for human approval, and applies approved descriptions.
# MAGIC
# MAGIC ## Workflow
# MAGIC 1. **Generate** — AI creates descriptions for all tables/columns
# MAGIC 2. **Review** — Humans review and approve/edit in the review table
# MAGIC 3. **Apply** — Approved descriptions are written to Unity Catalog

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Configuration

# COMMAND ----------

CATALOG = "{catalog_name}"
SCHEMA = "{schema_name}"
REVIEW_TABLE = f"{{CATALOG}}.{{SCHEMA}}._ai_description_reviews"
MODEL = "{app_config.serving_endpoint}"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Create Review Table

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {{REVIEW_TABLE}} (
    full_table_name STRING,
    item_type STRING COMMENT 'TABLE or COLUMN',
    item_name STRING COMMENT 'Column name, or table name for table-level',
    current_description STRING,
    ai_description STRING,
    final_description STRING COMMENT 'Edited by reviewer, or same as ai_description',
    status STRING COMMENT 'pending, approved, rejected, applied',
    reviewed_by STRING,
    reviewed_at TIMESTAMP,
    applied_at TIMESTAMP,
    generated_at TIMESTAMP
) USING DELTA
COMMENT 'AI-generated description review queue'
""")
print(f"Review table ready: {{REVIEW_TABLE}}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Generate AI Descriptions for All Tables

# COMMAND ----------

system_prompt = """You are a data catalog documentation expert. Generate clear, concise,
business-friendly descriptions for database tables and columns.

Rules:
- Table descriptions: 1-3 sentences explaining what the table stores, its business purpose,
  and key relationships.
- Column descriptions: 1 sentence explaining what the column represents in business terms.
- Use plain English, avoid jargon where possible.
- Be specific about data types, units, and formats when relevant.
- Do not include the column name or type in the description — the reader already sees those.
- Return valid JSON only, no markdown fences."""
{rules_block}
# COMMAND ----------

from pyspark.sql.functions import lit, current_timestamp
import json

tables = spark.sql(f"SHOW TABLES IN {{CATALOG}}.{{SCHEMA}}").collect()
print(f"Found {{len(tables)}} tables to process")

for row in tables:
    table_name = row.tableName
    full_name = f"{{CATALOG}}.{{SCHEMA}}.{{table_name}}"

    # Skip the review table itself
    if table_name == "_ai_description_reviews":
        continue

    print(f"\\nProcessing: {{full_name}}")

    # Get column info
    columns = spark.sql(f"DESCRIBE TABLE {{full_name}}").collect()
    col_info = "\\n".join(
        f"  - {{c.col_name}} ({{c.data_type}})"
        for c in columns
        if not c.col_name.startswith("#")
    )

    # Get current table comment
    table_detail = spark.sql(f"DESCRIBE TABLE EXTENDED {{full_name}}").collect()
    current_comment = ""
    for r in table_detail:
        if r.col_name == "Comment":
            current_comment = r.data_type or ""

    prompt = f"""Generate descriptions for this Unity Catalog table and its columns.

Table: {{full_name}}
Current description: {{current_comment or '(none)'}}

Columns:
{{col_info}}

Return JSON: {{"table_description": "...", "column_descriptions": {{"col": "desc", ...}}}}"""

    # Call AI using ai_query
    result_df = spark.sql(f"""
        SELECT ai_query(
            '{{MODEL}}',
            '{{prompt.replace("'", "''")}}',
            'returnType', 'STRING'
        ) as response
    """)
    response_text = result_df.collect()[0].response

    # Parse JSON
    try:
        if response_text.startswith("```"):
            response_text = response_text.split("\\n", 1)[1]
            if response_text.endswith("```"):
                response_text = response_text[:-3].strip()
        suggestions = json.loads(response_text)
    except json.JSONDecodeError:
        print(f"  ERROR: Could not parse AI response for {{full_name}}")
        continue

    # Insert table-level suggestion
    rows = [(
        full_name, "TABLE", table_name,
        current_comment, suggestions.get("table_description", ""),
        suggestions.get("table_description", ""), "pending"
    )]

    # Insert column-level suggestions
    for c in columns:
        if c.col_name.startswith("#"):
            continue
        col_desc = suggestions.get("column_descriptions", {{}}).get(c.col_name, "")
        if col_desc:
            rows.append((
                full_name, "COLUMN", c.col_name,
                "", col_desc, col_desc, "pending"
            ))

    review_df = spark.createDataFrame(rows, [
        "full_table_name", "item_type", "item_name",
        "current_description", "ai_description",
        "final_description", "status"
    ]).withColumn("generated_at", current_timestamp())

    review_df.write.mode("append").saveAsTable(REVIEW_TABLE)
    print(f"  Added {{len(rows)}} suggestions to review table")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Review Pending Descriptions
# MAGIC
# MAGIC Query the review table, edit `final_description` and set `status` to `approved` or `rejected`.

# COMMAND ----------

display(spark.sql(f"""
    SELECT full_table_name, item_type, item_name,
           current_description, ai_description, final_description, status
    FROM {{REVIEW_TABLE}}
    WHERE status = 'pending'
    ORDER BY full_table_name, item_type DESC, item_name
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ### Approve all pending (optional — remove this if you want manual review)

# COMMAND ----------

# Uncomment to auto-approve all pending:
# spark.sql(f"""
#     UPDATE {{REVIEW_TABLE}}
#     SET status = 'approved',
#         reviewed_by = current_user(),
#         reviewed_at = current_timestamp()
#     WHERE status = 'pending'
# """)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: Apply Approved Descriptions to Unity Catalog

# COMMAND ----------

approved = spark.sql(f"""
    SELECT * FROM {{REVIEW_TABLE}}
    WHERE status = 'approved'
    ORDER BY full_table_name, item_type DESC
""").collect()

print(f"Applying {{len(approved)}} approved descriptions...")

for row in approved:
    try:
        if row.item_type == "TABLE":
            escaped = row.final_description.replace("'", "\\\\'")
            spark.sql(f"COMMENT ON TABLE {{row.full_table_name}} IS '{{escaped}}'")
        else:
            escaped = row.final_description.replace("'", "\\\\'")
            spark.sql(f"ALTER TABLE {{row.full_table_name}} ALTER COLUMN `{{row.item_name}}` COMMENT '{{escaped}}'")

        # Mark as applied
        spark.sql(f"""
            UPDATE {{REVIEW_TABLE}}
            SET status = 'applied', applied_at = current_timestamp()
            WHERE full_table_name = '{{row.full_table_name}}'
              AND item_type = '{{row.item_type}}'
              AND item_name = '{{row.item_name}}'
              AND status = 'approved'
        """)
        print(f"  Applied: {{row.item_type}} {{row.full_table_name}}.{{row.item_name}}")
    except Exception as e:
        print(f"  FAILED: {{row.item_type}} {{row.full_table_name}}.{{row.item_name}}: {{e}}")

print("\\nDone!")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Audit: View All Applied Descriptions

# COMMAND ----------

display(spark.sql(f"""
    SELECT full_table_name, item_type, item_name,
           current_description, ai_description, final_description,
           status, reviewed_by, reviewed_at, applied_at
    FROM {{REVIEW_TABLE}}
    ORDER BY applied_at DESC
"""))
'''
    return notebook
