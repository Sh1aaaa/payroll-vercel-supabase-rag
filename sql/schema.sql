-- Run this in Supabase SQL Editor.
create extension if not exists vector;
create extension if not exists pgcrypto;

create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  full_name text,
  role text not null default 'employee' check (role in ('super_admin','hr','employee')),
  approved boolean not null default false,
  created_at timestamptz not null default now()
);

create or replace function public.handle_new_user() returns trigger language plpgsql security definer set search_path=public as $$
begin
  insert into public.profiles(id, full_name) values(new.id, coalesce(new.raw_user_meta_data->>'full_name','')) on conflict (id) do nothing;
  return new;
end; $$;
drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created after insert on auth.users for each row execute procedure public.handle_new_user();

create table if not exists public.employees (
  id uuid primary key default gen_random_uuid(),
  profile_id uuid unique references public.profiles(id) on delete set null,
  employee_no text unique not null,
  full_name text not null,
  department text,
  employee_type text not null check(employee_type in ('regular','temporary','contractual','casual','ebjo','cos','part_time')),
  monthly_salary numeric(14,2) not null default 0,
  hourly_rate numeric(14,2) not null default 0,
  standard_hours numeric(5,2) not null default 8,
  workdays_per_month numeric(5,2) not null default 22,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

create table if not exists public.dtr_entries (
  id uuid primary key default gen_random_uuid(),
  employee_id uuid not null references public.employees(id) on delete cascade,
  work_date date not null,
  time_in timestamptz,
  time_out timestamptz,
  status text not null,
  payable_hours numeric(6,2) not null default 0,
  reason text,
  requires_review boolean not null default false,
  import_batch uuid,
  reviewed_by uuid references public.profiles(id),
  reviewed_at timestamptz,
  created_at timestamptz not null default now(),
  unique(employee_id, work_date)
);
create index if not exists idx_dtr_emp_date on public.dtr_entries(employee_id, work_date);
create index if not exists idx_dtr_review on public.dtr_entries(requires_review) where requires_review=true;

create table if not exists public.employee_deductions (
  id uuid primary key default gen_random_uuid(),
  employee_id uuid not null references public.employees(id) on delete cascade,
  kind text not null,
  name text not null,
  amount numeric(14,2) not null default 0,
  split_rule text not null default 'whole' check(split_rule in ('whole','half_each')),
  active boolean not null default true,
  notes text,
  created_at timestamptz not null default now()
);

create table if not exists public.payroll_runs (
  id uuid primary key default gen_random_uuid(),
  start_date date not null,
  end_date date not null,
  cutoff_no smallint not null check(cutoff_no in (1,2)),
  status text not null default 'draft' check(status in ('draft','approved','released','void')),
  created_by uuid references public.profiles(id),
  approved_by uuid references public.profiles(id),
  approved_at timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists public.payroll_items (
  id uuid primary key default gen_random_uuid(),
  payroll_run_id uuid not null references public.payroll_runs(id) on delete cascade,
  employee_id uuid not null references public.employees(id),
  gross_pay numeric(14,2) not null default 0,
  total_deductions numeric(14,2) not null default 0,
  net_pay numeric(14,2) not null default 0,
  attendance_summary jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique(payroll_run_id, employee_id)
);

create table if not exists public.payroll_item_deductions (
  id uuid primary key default gen_random_uuid(),
  payroll_item_id uuid not null references public.payroll_items(id) on delete cascade,
  deduction_id uuid references public.employee_deductions(id) on delete set null,
  name text not null,
  amount numeric(14,2) not null
);

create table if not exists public.complaints (
  id uuid primary key default gen_random_uuid(),
  employee_id uuid not null references public.employees(id) on delete cascade,
  submitted_by uuid references public.profiles(id),
  subject text not null,
  complaint_text text not null,
  status text not null default 'submitted' check(status in ('submitted','assessed','under_review','resolved','rejected')),
  resolution text,
  resolved_by uuid references public.profiles(id),
  resolved_at timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists public.knowledge_chunks (
  id uuid primary key default gen_random_uuid(),
  title text not null,
  content text not null,
  source text,
  embedding vector(1536),
  created_at timestamptz not null default now()
);
create index if not exists knowledge_embedding_hnsw on public.knowledge_chunks using hnsw (embedding vector_cosine_ops);

create or replace function public.match_knowledge(query_embedding vector(1536), match_threshold float, match_count int)
returns table(id uuid, title text, content text, source text, similarity float)
language sql stable as $$
  select kc.id, kc.title, kc.content, kc.source, 1 - (kc.embedding <=> query_embedding) as similarity
  from public.knowledge_chunks kc
  where 1 - (kc.embedding <=> query_embedding) > match_threshold
  order by kc.embedding <=> query_embedding
  limit match_count;
$$;

create table if not exists public.complaint_assessments (
  id uuid primary key default gen_random_uuid(),
  complaint_id uuid not null references public.complaints(id) on delete cascade,
  assessment jsonb not null,
  retrieved_knowledge_ids uuid[] not null default '{}',
  model text,
  created_at timestamptz not null default now()
);

create table if not exists public.audit_logs (
  id bigint generated always as identity primary key,
  actor_id uuid references public.profiles(id),
  action text not null,
  entity_type text,
  entity_id text,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

-- The Flask server uses the service-role key for DB work and performs role checks server-side.
-- Keep RLS enabled as an additional guard against direct browser access with the anon key.
alter table public.profiles enable row level security;
alter table public.employees enable row level security;
alter table public.dtr_entries enable row level security;
alter table public.employee_deductions enable row level security;
alter table public.payroll_runs enable row level security;
alter table public.payroll_items enable row level security;
alter table public.payroll_item_deductions enable row level security;
alter table public.complaints enable row level security;
alter table public.knowledge_chunks enable row level security;
alter table public.complaint_assessments enable row level security;
alter table public.audit_logs enable row level security;

create policy "profile reads self" on public.profiles for select to authenticated using (id=auth.uid());
create policy "employee reads own employee row" on public.employees for select to authenticated using (profile_id=auth.uid());
create policy "employee reads own dtr" on public.dtr_entries for select to authenticated using (employee_id in (select id from public.employees where profile_id=auth.uid()));
create policy "employee reads own payroll" on public.payroll_items for select to authenticated using (employee_id in (select id from public.employees where profile_id=auth.uid()));
create policy "employee reads own complaints" on public.complaints for select to authenticated using (employee_id in (select id from public.employees where profile_id=auth.uid()));
