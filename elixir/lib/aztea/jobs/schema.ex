defmodule Aztea.Jobs.Schema do
  @moduledoc """
  Read-only Ecto schema for the `jobs` table.

  The jobs table is owned by the Python server — Elixir only reads and updates
  status/lease columns. Column names must match the PostgreSQL schema exactly.
  Run `\d jobs` in psql to verify if adding fields.

  # OWNS: Ecto struct mapping for the jobs table
  # NOT OWNS: job creation, payments, auth (Python server owns those)
  # INVARIANTS: never add Ecto migrations — schema is managed by Python migrate.py
  """

  use Ecto.Schema

  @primary_key {:job_id, :string, autogenerate: false}
  @timestamps_opts false

  schema "jobs" do
    field :status, :string
    field :agent_id, :string
    field :client_id, :string
    # Payload fields — real column names differ from the original scaffold
    field :input_payload, :string
    field :output_payload, :string
    field :error_message, :string
    field :price_cents, :integer
    field :charge_tx_id, :string
    field :created_at, :string
    field :updated_at, :string
    field :completed_at, :string
    field :lease_expires_at, :string
    field :last_heartbeat_at, :string
    field :attempt_count, :integer
    field :max_attempts, :integer
    field :retry_count, :integer
    field :parent_job_id, :string
    field :parent_cascade_policy, :string
    field :batch_id, :string
    field :clarification_timeout_seconds, :integer
    field :clarification_timeout_policy, :string
    field :clarification_requested_at, :string
    field :clarification_deadline_at, :string
    field :output_verification_window_seconds, :integer
    field :output_verification_status, :string
    field :output_verification_deadline_at, :string
  end
end
