# Staging rollout

Order matters: the new code reads `Country.code`, so the migrations must run
before the web containers serve the new code, or every country request returns 500.

1. **Back up** the database.
2. **Migrate**: `python manage.py migrate` (country 0004 adds `code`, 0005 fills
   it from the country names; event, model and risk_information add the queue and
   Event-scoping tables). Check `python manage.py showmigrations` shows no `[ ]`.
3. **Verify codes**: `python manage.py shell -c "from country.models import Country; print(list(Country.objects.values_list('short_name','code')))"`.
   Every live country needs a code (CK, VU, TO, WS...). Set any that are blank in the admin.
4. **Deploy the web containers**, then check `GET /partner_api/v1/country/` returns 200.
5. **Set the runner environment** (never commit these): the Partner API token and
   `RISKSCAPE_TOKEN`, ideally from a service account rather than a personal one.
6. **Create the pipelines**: `python manage.py setup_tc_pipelines`.
7. **Start the worker** (`docker compose up -d runner`, or
   `python manage.py run_model_queue`), then confirm
   `python manage.py check_runner_health` reports a recent heartbeat.
8. **Smoke test** with one known advisory before enabling it for real tracks.

Rollback: stop the runner first, then redeploy the previous web image. The
migrations only add columns and tables, so they can stay applied.
