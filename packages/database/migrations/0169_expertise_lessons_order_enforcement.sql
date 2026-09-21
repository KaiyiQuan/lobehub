ALTER TABLE "expertise_lessons" ADD COLUMN IF NOT EXISTS "sort_order" integer DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE "expertise_lessons" ADD COLUMN IF NOT EXISTS "enforcement" text DEFAULT 'remind' NOT NULL;
