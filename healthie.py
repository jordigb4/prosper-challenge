"""Healthie EHR integration module.

This module provides functions to interact with Healthie for patient management
and appointment scheduling.
"""

import os

from playwright.async_api import async_playwright, Browser, Page
from loguru import logger
from difflib import SequenceMatcher

_browser: Browser | None = None
_page: Page | None = None

# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------

def _similarity(a: str, b: str) -> float:
    """Return a 0–1 similarity score between two strings (case-insensitive)."""
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _parse_healthie_dob(raw: str) -> str:
    """Convert Healthie's displayed DOB (e.g. 'Nov 9, 2016') to YYYY-MM-DD."""
    from datetime import datetime
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw.strip()


def _normalise_dob(raw: str) -> str:
    """Normalise caller-provided DOB to YYYY-MM-DD for comparison."""
    import re
    from datetime import datetime

    raw = raw.strip()
    # Already ISO
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw
    # MM/DD/YYYY
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", raw)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    # Named-month formats
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw

async def login_to_healthie() -> Page:
    """Log into Healthie and return an authenticated page instance.

    This function handles the login process using credentials from environment
    variables. The browser and page instances are stored for reuse by other
    functions in this module.

    Returns:
        Page: An authenticated Playwright Page instance ready for use.

    Raises:
        ValueError: If required environment variables are missing.
        Exception: If login fails for any reason.
    """
    global _browser, _page

    email = os.environ.get("HEALTHIE_EMAIL")
    password = os.environ.get("HEALTHIE_PASSWORD")

    if not email or not password:
        raise ValueError("HEALTHIE_EMAIL and HEALTHIE_PASSWORD must be set in environment variables")

    if _page is not None:
        logger.info("Using existing Healthie session")
        return _page

    logger.info("Logging into Healthie...")
    playwright = await async_playwright().start()
    _browser = await playwright.chromium.launch(headless=True)
    _page = await _browser.new_page()

    await _page.goto("https://secure.gethealthie.com/users/sign_in", wait_until="domcontentloaded")
    
    # Wait for the email input to be visible
    email_input = _page.locator('input[name="identifier"]')
    await email_input.wait_for(state="visible", timeout=30000)
    await email_input.fill(email)

    # Find and click the Log In button
    submit_button = _page.locator('button:has-text("Log In")')
    await submit_button.wait_for(state="visible", timeout=30000)
    await submit_button.click()
    
    # Wait for password input
    password_input = _page.locator('input[name="password"]')
    await password_input.wait_for(state="visible", timeout=30000)
    await password_input.fill(password)
    
    # Find and click the Log In button
    submit_button = _page.locator('button:has-text("Log In")')
    await submit_button.wait_for(state="visible", timeout=30000)
    await submit_button.click()

    # Passkey workaround: Continue to app
    submit_button = _page.locator('button:has-text("Continue to app")')
    await submit_button.wait_for(state="visible", timeout=30000)
    await submit_button.click()
    
    # Wait for navigation after login
    await _page.wait_for_timeout(3000)
    
    # Check if we've navigated away from the sign-in page
    current_url = _page.url
    if "sign_in" in current_url:
        raise Exception("Login may have failed - still on sign-in page")

    logger.info("Successfully logged into Healthie")
    return _page


async def find_patient(name: str, date_of_birth: str) -> dict | None:
    """Find a patient in Healthie by name and date of birth.

    Args:
        name: The patient's full name.
        date_of_birth: The patient's date of birth in a format that Healthie accepts.

    Returns:
        dict | None: A dictionary containing patient information if found,
            including at least a 'patient_id' field. Returns None if the patient
            is not found or if an error occurs.

    Example return value:
        {
            "patient_id": "12345",
            "name": "John Doe",
            "date_of_birth": "1990-01-15",
            ...
        }
    """
    # TODO: Implement patient search functionality using Playwright
    page = await login_to_healthie()
    dob_iso = _normalise_dob(date_of_birth)
    logger.info(f"Searching for patient: name={name!r} dob={dob_iso!r}")

    # ------------------------------------------------------------------
    # Step 1 — Type into the header search box
    # ------------------------------------------------------------------
    search_input = page.locator('input[name="keywords"]')
    await search_input.wait_for(state="visible", timeout=15_000)
    await search_input.click()
    await search_input.fill(name)
    logger.debug("Search query entered, waiting for autocomplete results…")


    # ------------------------------------------------------------------
    # Step 2 — Wait for at least one result row to appear
    # ------------------------------------------------------------------
    result_rows = page.locator('[data-testid="header-client-result"]')
    try:
        await result_rows.first.wait_for(state="visible", timeout=10_000)
    except Exception:
        logger.warning(f"No autocomplete results appeared for query {name!r}")
        return None

    # ------------------------------------------------------------------
    # Step 3 — Collect all result rows, fuzzy-match name AND verify DOB
    # The row text contains both: e.g. 'John Wright Doe  (11/9/2016)'
    # Patient ID is in the href of the "View Profile" link — no page visit needed.
    # ------------------------------------------------------------------
    import re as _re

    row_count = await result_rows.count()
    logger.debug(f"Found {row_count} autocomplete result(s)")

    SIMILARITY_THRESHOLD = 0.6

    for i in range(row_count):
        row = result_rows.nth(i)
        candidate_name = (
            await row.locator('[data-testid="header-client-result-name"]').text_content() or ""
        ).strip()
        import re as _re
        candidate_name_clean = _re.sub(r"\s*\(.*?\)", "", candidate_name).strip()
        score = _similarity(name, candidate_name_clean)
        logger.debug(f"  Row {i}: {candidate_name!r} (clean: {candidate_name_clean!r}) — similarity {score:.2f}")

        if score < SIMILARITY_THRESHOLD:
            continue

        # Extract DOB from the row's full text, e.g. "(11/9/2016)"
        row_text = (await row.text_content() or "").strip()
        dob_match = _re.search(r"\((\d{1,2}/\d{1,2}/\d{4})\)", row_text)
        if not dob_match:
            logger.debug(f"  Row {i}: no DOB found in row text {row_text!r}, skipping")
            continue

        row_dob_iso = _normalise_dob(dob_match.group(1))
        if row_dob_iso != dob_iso:
            logger.debug(f"  Row {i}: DOB mismatch ({row_dob_iso!r} != {dob_iso!r}), skipping")
            continue

        # Name and DOB both match — grab patient ID from the href
        href = await row.locator('[data-testid="view-profile"]').get_attribute("href") or ""
        id_match = _re.search(r"/users/(\d+)", href)
        if not id_match:
            logger.warning(f"  Row {i}: could not parse patient ID from href {href!r}")
            continue

        patient_id = id_match.group(1)
        profile_url = f"https://secure.gethealthie.com{href}"
        logger.info(f"Patient matched — id={patient_id!r} name={candidate_name!r} dob={row_dob_iso!r}")
        return {
            "patient_id": patient_id,
            "name": candidate_name,
            "date_of_birth": row_dob_iso,
            "profile_url": profile_url,
        }

    logger.warning(f"No result matched both name similarity and DOB for {name!r} / {dob_iso!r}")
    return None

async def create_appointment(patient_id: str, date: str, time: str) -> dict | None:
    """Create an appointment in Healthie for the specified patient.

    Args:
        patient_id: The unique identifier for the patient in Healthie.
        date: The desired appointment date in a format that Healthie accepts.
        time: The desired appointment time in a format that Healthie accepts.

    Returns:
        dict | None: A dictionary containing appointment information if created
            successfully, including at least an 'appointment_id' field.
            Returns None if appointment creation fails.

    Example return value:
        {
            "appointment_id": "67890",
            "patient_id": "12345",
            "date": "2026-02-15",
            "time": "10:00 AM",
            ...
        }
    """
    # TODO: Implement appointment creation functionality using Playwright
    # 1. Ensure you're logged in by calling login_to_healthie()
    # 2. Navigate to the appointment creation page for the patient
    # 3. Fill in the date and time fields
    # 4. Submit the appointment creation form
    # 5. Verify the appointment was created successfully
    # 6. Return appointment information
    # 7. Handle errors (e.g., time slot unavailable, invalid date/time)
    pass
