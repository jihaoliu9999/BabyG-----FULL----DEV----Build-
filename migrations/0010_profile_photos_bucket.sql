-- babyg :: 0010 :: profile photo storage bucket
-- Provisions the Supabase Storage bucket that backs creator profile photos.
-- The bucket is public-read so <img src="..."> works without signed URLs.
-- Writes (insert/update/delete) are restricted to the service_role key,
-- which only our backend holds — so RLS policies on storage.objects are
-- intentionally NOT added. The backend validates uploads before writing.

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'profile-photos',
  'profile-photos',
  true,
  6291456, -- 6 MiB hard cap at the storage layer (defense in depth; the
           -- route caps raw uploads at 5 MiB before we recompress)
  array['image/jpeg', 'image/png', 'image/webp']
)
on conflict (id) do update set
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;
