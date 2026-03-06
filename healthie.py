"""Healthie EHR integration module.

This module provides functions to interact with Healthie for patient management
and appointment scheduling.
"""

import os

from playwright.async_api import async_playwright, Browser, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from difflib import SequenceMatcher
from loguru import logger
import asyncio
from datetime import datetime

_browser: Browser | None = None
_page: Page | None = None
_playwright_ctx = None  # holds the active Stealth context manager

async def login_to_healthie() -> Page:
    global _browser, _page, _playwright_ctx

    email = os.environ.get("HEALTHIE_EMAIL")
    password = os.environ.get("HEALTHIE_PASSWORD")

    if not email or not password:
        raise ValueError("HEALTHIE_EMAIL and HEALTHIE_PASSWORD must be set in environment variables")

    logger.info("Logging into Healthie...")

    from playwright_stealth import Stealth

    # Enter and store the context manager so it stays alive across calls
    _playwright_ctx = Stealth().use_async(async_playwright())
    p = await _playwright_ctx.__aenter__()

    _browser = await p.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )

    context = await _browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        timezone_id="America/New_York",
    )

    _page = await context.new_page()

    await _page.goto("https://secure.gethealthie.com/users/sign_in", wait_until="domcontentloaded")

    email_input = _page.locator('input[name="identifier"]')
    await email_input.wait_for(state="visible", timeout=30000)
    await email_input.fill(email)

    submit_button = _page.locator('button:has-text("Log In")')
    await submit_button.wait_for(state="visible", timeout=30000)
    await submit_button.click()

    password_input = _page.locator('input[name="password"]')
    await password_input.wait_for(state="visible", timeout=30000)
    await password_input.fill(password)

    submit_button = _page.locator('button:has-text("Log In")')
    await submit_button.wait_for(state="visible", timeout=30000)
    await submit_button.click()

    submit_button = _page.locator('button:has-text("Continue to app")')
    await submit_button.wait_for(state="visible", timeout=30000)
    await submit_button.click()

    await _page.wait_for_timeout(3000)

    if "sign_in" in _page.url:
        raise Exception("Login may have failed - still on sign-in page")

    logger.info(f"Successfully logged into Healthie (landed on {_page.url})")
    return _page

#  async def login_to_healthie() -> Page:
#     """Log into Healthie and return an authenticated page instance.
#
#     This function handles the login process using credentials from environment
#     variables. The browser and page instances are stored for reuse by other
#     functions in this module.
#
#     Returns:
#         Page: An authenticated Playwright Page instance ready for use.
#
#     Raises:
#         ValueError: If required environment variables are missing.
#         Exception: If login fails for any reason.
#     """
#     global _browser, _page
#
#     email = os.environ.get("HEALTHIE_EMAIL")
#     password = os.environ.get("HEALTHIE_PASSWORD")
#
#     if not email or not password:
#         raise ValueError("HEALTHIE_EMAIL and HEALTHIE_PASSWORD must be set in environment variables")
#
#     if _page is not None:
#         logger.info("Using existing Healthie session")
#         return _page
#
#     logger.info("Logging into Healthie...")
#     playwright = await async_playwright().start()
#     _browser = await playwright.chromium.launch(headless=True)
#     _page = await _browser.new_page()
#
#     await _page.goto("https://secure.gethealthie.com/users/sign_in", wait_until="domcontentloaded")
#
#     # Wait for the email input to be visible
#     email_input = _page.locator('input[name="identifier"]')
#     await email_input.wait_for(state="visible", timeout=30000)
#     await email_input.fill(email)
#
#     # Find and click the Log In button
#     submit_button = _page.locator('button:has-text("Log In")')
#     await submit_button.wait_for(state="visible", timeout=30000)
#     await submit_button.click()
#
#     # Wait for password input
#     password_input = _page.locator('input[name="password"]')
#     await password_input.wait_for(state="visible", timeout=30000)
#     await password_input.fill(password)
#
#     # Find and click the Log In button
#     submit_button = _page.locator('button:has-text("Log In")')
#     await submit_button.wait_for(state="visible", timeout=30000)
#     await submit_button.click()
#
#     # Passkey workaround: Continue to app
#     submit_button = _page.locator('button:has-text("Continue to app")')
#     await submit_button.wait_for(state="visible", timeout=30000)
#     await submit_button.click()
#
#     # Wait for navigation after login
#     await _page.wait_for_timeout(3000)
#
#     # Check if we've navigated away from the sign-in page
#     current_url = _page.url
#     if "sign_in" in current_url:
#         raise Exception("Login may have failed - still on sign-in page")
#
#     logger.info("Successfully logged into Healthie")
#     return _page


async def find_patient(name: str, date_of_birth: str) -> dict:
    """Find a patient in Healthie by name and date of birth."""

    logger.info(f"[find_patient] Starting search for patient name={name!r}, dob={date_of_birth!r}")

    try:
        page = await login_to_healthie()
        logger.debug("[find_patient] Successfully logged into Healthie")

        search_input = page.locator('input[name="keywords"]')
        await search_input.wait_for(state="visible", timeout=15000)
        logger.debug(f"[find_patient] Filling search input with name={name!r}")
        await search_input.fill(name)

        result_locator = page.locator('[data-testid="header-client-result"]')

        try:
            await result_locator.first.wait_for(state="visible", timeout=8000)
            logger.debug("[find_patient] Search results appeared")
        except Exception:
            logger.warning(f"[find_patient] No results found for name={name!r}")
            await search_input.fill("")
            return {
                "success": False,
                "patient": None,
                "reason": "no_results_for_name"
            }

        results = await result_locator.all()
        logger.info(f"[find_patient] Found {len(results)} result(s) for name={name!r}")

        try:
            dob_dt = datetime.strptime(date_of_birth, "%Y-%m-%d")
            dob_display = f"{dob_dt.month}/{dob_dt.day}/{dob_dt.year}"
            logger.debug(f"[find_patient] Parsed DOB: {date_of_birth!r} → display={dob_display!r}")
        except ValueError:
            logger.warning(f"[find_patient] Could not parse date_of_birth={date_of_birth!r} as %Y-%m-%d, using as-is")
            dob_display = date_of_birth

        for i, result in enumerate(results):
            name_div = result.locator('[data-testid="header-client-result-name"]')
            raw_text = (await name_div.inner_text()).strip()
            logger.debug(f"[find_patient] Result[{i}] raw_text={raw_text!r}")

            if "(" in raw_text and ")" in raw_text:
                display_name = raw_text[:raw_text.rfind("(")].strip()
                display_dob = raw_text[raw_text.rfind("(") + 1: raw_text.rfind(")")].strip()
            else:
                display_name = raw_text
                display_dob = ""

            logger.debug(f"[find_patient] Result[{i}] parsed name={display_name!r}, dob={display_dob!r}")

            if display_dob == dob_display:
                logger.info(f"[find_patient] DOB match found for result[{i}]: name={display_name!r}")
                view_profile_link = result.locator('[data-testid="view-profile"]')
                href = await view_profile_link.get_attribute("href")
                patient_id = href.rstrip("/").split("/")[-1]
                logger.info(f"[find_patient] Resolved patient_id={patient_id!r} from href={href!r}")

                await search_input.fill("")

                return {
                    "success": True,
                    "patient": {
                        "patient_id": patient_id,
                        "name": display_name,
                        "date_of_birth": date_of_birth
                    },
                    "reason": None
                }

        logger.warning(
            f"[find_patient] No DOB match for name={name!r}; "
            f"expected dob_display={dob_display!r} but none of the {len(results)} result(s) matched"
        )
        await search_input.fill("")

        return {
            "success": False,
            "patient": None,
            "reason": "dob_mismatch"
        }

    except Exception as exc:
        logger.exception(f"[find_patient] Unexpected error for name={name!r}, dob={date_of_birth!r}: {exc}")

        return {
            "success": False,
            "patient": None,
            "reason": "system_error"
        }


async def create_appointment(patient_id: str, date: str, time: str) -> dict | None:
    """Create an appointment in Healthie for the specified patient."""

    logger.info(f"[create_appointment] Starting for patient_id={patient_id!r}, date={date!r}, time={time!r}")

    try:
        page = await login_to_healthie()
        logger.debug("[create_appointment] Successfully logged into Healthie")

        profile_url = f"https://secure.gethealthie.com/users/{patient_id}"
        logger.info(f"[create_appointment] Navigating to {profile_url}")
        await page.goto(profile_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        timezone = page.get_by_text("No, do not change my setting")
        add_appt_button = page.get_by_test_id("add-appointment-button")

        logger.debug("[create_appointment] Waiting for add-appointment-button to become visible")
        await add_appt_button.first.wait_for(state="visible", timeout=15000)

        if await timezone.count() > 0 and await timezone.first.is_visible():
            logger.debug("[create_appointment] Timezone confirmation dialog detected; dismissing")
            await timezone.first.click()

        logger.debug("[create_appointment] Clicking add-appointment-button")
        await add_appt_button.first.click()
        await page.wait_for_timeout(1500)

        # Select appointment type
        appt_type_container = page.locator(".appointment_type_id")
        logger.debug("[create_appointment] Waiting for appointment type dropdown")
        await appt_type_container.wait_for(state="visible", timeout=10000)
        await appt_type_container.click()
        await page.wait_for_timeout(500)

        first_option = page.locator(
            ".appointment_type_id .css-4ljt47-MenuList > div, .appointment_type_id [class*='option']"
        ).first
        await first_option.wait_for(state="visible", timeout=5000)
        first_option_text = await first_option.inner_text()
        logger.info(f"[create_appointment] Selecting first appointment type: {first_option_text.strip()!r}")
        await first_option.click()
        await page.wait_for_timeout(500)

        # Parse and fill date
        try:
            date_dt = datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            logger.error(f"[create_appointment] Invalid date format {date!r}, expected %Y-%m-%d: {exc}")
            return {"success": False, "appointment": None, "reason": "system_error"}

        date_display = date_dt.strftime("%B %-d, %Y")
        logger.debug(f"[create_appointment] Filling date field: {date!r} → {date_display!r}")
        date_input = page.locator('input[name="date"]')
        await date_input.wait_for(state="visible", timeout=10000)
        await date_input.click()
        await date_input.fill(date_display)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(500)

        # Parse and fill time
        try:
            time_dt = datetime.strptime(time, "%H:%M")
        except ValueError as exc:
            logger.error(f"[create_appointment] Invalid time format {time!r}, expected %H:%M: {exc}")
            return {"success": False, "appointment": None, "reason": "system_error"}

        time_display = time_dt.strftime("%-I:%M %p")
        logger.debug(f"[create_appointment] Filling time field: {time!r} → {time_display!r}")
        time_input = page.locator('input[id="time"]')
        await time_input.wait_for(state="visible", timeout=10000)
        await time_input.click()
        await time_input.fill(time_display)
        await time_input.press("Enter")
        await page.wait_for_timeout(500)

        flash = page.locator('div[data-testid="flash-message"]:has-text("another event")')

        try:
            await flash.wait_for(state="visible", timeout=3000)

            logger.warning(
                f"[create_appointment] Time slot conflict for patient_id={patient_id!r} "
                f"on {date!r} at {time!r}"
            )
            await page.screenshot(path="page.png")

            return {"success": False, "appointment": None, "reason": "unavailable_time_slot"}

        except PlaywrightTimeoutError:
            logger.debug("[create_appointment] No conflict message detected")

        # Submit the form
        submit_button = page.locator('[data-testid="primaryButton"]:has-text("Add appointment")')
        logger.debug("[create_appointment] Waiting for submit button")
        await submit_button.wait_for(state="visible", timeout=10000)
        logger.info(
            f"[create_appointment] Submitting appointment for patient_id={patient_id!r} "
            f"on {date_display!r} at {time_display!r}"
        )
        await submit_button.click()
        await page.wait_for_timeout(2500)

        # Verify appointment appears in the appointment tab
        appt_tab = page.locator(".appointment-tab-contents .row")
        try:
            await appt_tab.first.wait_for(state="visible", timeout=5000)
            first_appt_text = (await appt_tab.first.locator("._info_ql3jo_41").inner_text()).strip()
            logger.info(f"[create_appointment] Verified appointment in tab: {first_appt_text!r}")
        except Exception:
            logger.warning(
                "[create_appointment] Could not verify appointment in tab after submit; assuming success"
            )

        logger.info(
            f"[create_appointment] Successfully created appointment for patient_id={patient_id!r} "
            f"on {date!r} at {time_display!r}"
        )
        return {
            "success": True,
            "appointment": {
                "patient_id": patient_id,
                "date": date,
                "time": time_display
            },
            "reason": None
        }

    except Exception as exc:
        logger.exception(
            f"[create_appointment] Unexpected error for patient_id={patient_id!r}, "
            f"date={date!r}, time={time!r}: {exc}"
        )
        return {"success": False, "appointment": None, "reason": "system_error"}