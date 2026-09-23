ALTER TABLE "environment_instances" ADD COLUMN IF NOT EXISTS "build_id" text;--> statement-breakpoint
ALTER TABLE "environment_instances" ADD COLUMN IF NOT EXISTS "build_error" text;
