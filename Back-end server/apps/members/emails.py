"""
emails.py -- MEMBERS module

Handles all outgoing emails for the members app:

    1. send_member_welcome_email()  -> sent the moment a member is created.
       Tells them which library added them and asks them to confirm their
       email address before their membership becomes active.

    2. send_member_active_email()   -> sent right after the member clicks
       the confirmation link and their status flips inactive -> active.

We use the `resend` Python SDK directly (rather than Django's send_mail()),
mirroring the exact same pattern already used in apps/users/emails.py, so
anyone reading both files sees the same shape and doesn't have to learn two
different ways of sending mail in this codebase.
"""

import resend
from django.conf import settings

# Setting resend.api_key here (at import time, module level) means this
# only runs ONCE, the first time Python imports this file — not on every
# single email we send. All resend.Emails.send(...) calls below reuse it.
resend.api_key = settings.RESEND_API_KEY


def send_member_welcome_email(
    member_name: str,
    library_name: str,
    to_email: str,
    confirmation_url: str,
) -> bool:
    """
    Sends the "you've been added as a member" + "please confirm your
    email" message. Fired once, right after a Member row is created
    (see MemberCreateView.perform_create in views.py).

    Args:
        member_name:       The member's display name — used to personalise
                            the email.
        library_name:      Name of the library the member was added to,
                            so the email makes sense to someone who might
                            be a member of more than one library.
        to_email:           The recipient's email address.
        confirmation_url:   Full frontend URL containing the member's uid
                             and the raw activation token, e.g.
                             http://localhost:3000/members/confirm?uid=...&token=...

    Returns:
        True if Resend accepted the email, False if sending failed.
        We deliberately don't raise here — a failed email shouldn't roll
        back the member record that was just created; the caller can
        decide whether/how to surface that (e.g. logging, a resend button).
    """
    try:
        resend.Emails.send(
            {
                "from": settings.RESEND_FROM_EMAIL,
                "to": [to_email],
                "subject": f"You've been added as a member of {library_name}",
                "html": _build_welcome_email_html(
                    member_name, library_name, confirmation_url
                ),
            }
        )
        return True
    except Exception as e:
        # In production this should go through a real logger instead of
        # print(), but print() keeps things simple while you're learning
        # and still shows up clearly in the console/server logs.
        print(f"[Email error] Failed to send welcome email to {to_email}: {e}")
        return False


def send_member_active_email(
    member_name: str,
    library_name: str,
    membership_no: str,
    to_email: str,
) -> bool:
    """
    Sends the "your account is now active" confirmation. Fired once,
    right after MemberActivationView successfully verifies the token and
    flips the member's status to "active" (see views.py).

    No URL/token needed here — this email is purely informational, it
    doesn't ask the member to do anything further.
    """
    try:
        resend.Emails.send(
            {
                "from": settings.RESEND_FROM_EMAIL,
                "to": [to_email],
                "subject": f"Your {library_name} membership is now active",
                "html": _build_active_email_html(member_name, library_name, membership_no),
            }
        )
        return True
    except Exception as e:
        print(f"[Email error] Failed to send activation-confirmed email to {to_email}: {e}")
        return False


# ---------------------------------------------------------------------------
# HTML BUILDERS
# ---------------------------------------------------------------------------
# Kept as small private (leading underscore) helper functions, separate
# from the send_*() functions above, purely so the HTML markup doesn't
# clutter up the "business logic" of deciding what to send and when.
# ---------------------------------------------------------------------------


def _build_welcome_email_html(
    member_name: str, library_name: str, confirmation_url: str
) -> str:
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="margin:0;padding:0;background-color:#f4f6f8;font-family:Arial,sans-serif;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f6f8;padding:40px 0;">
        <tr>
          <td align="center">
            <table width="560" cellpadding="0" cellspacing="0"
                   style="background:#ffffff;border-radius:8px;overflow:hidden;
                          box-shadow:0 1px 4px rgba(0,0,0,0.08);">

              <!-- Header -->
              <tr>
                <td style="background-color:#1E3A5F;padding:32px 40px;">
                  <p style="margin:0;font-size:20px;font-weight:bold;color:#ffffff;">
                    Welcome to {library_name}
                  </p>
                </td>
              </tr>

              <!-- Body -->
              <tr>
                <td style="padding:40px;">
                  <p style="margin:0 0 16px;font-size:16px;color:#1a1a1a;">
                    Hi {member_name},
                  </p>
                  <p style="margin:0 0 24px;font-size:15px;color:#444444;line-height:1.6;">
                    You've been added as a member of <strong>{library_name}</strong>.
                    Before your membership becomes active, please confirm your
                    email address by clicking the button below. This link
                    expires in <strong>24 hours</strong>.
                  </p>

                  <!-- CTA button -->
                  <table cellpadding="0" cellspacing="0" style="margin:0 0 32px;">
                    <tr>
                      <td style="background-color:#2E75B6;border-radius:6px;">
                        <a href="{confirmation_url}"
                           style="display:inline-block;padding:14px 32px;
                                  font-size:15px;font-weight:bold;
                                  color:#ffffff;text-decoration:none;">
                          Confirm my email address
                        </a>
                      </td>
                    </tr>
                  </table>

                  <p style="margin:0 0 8px;font-size:13px;color:#888888;">
                    If the button doesn't work, copy and paste this link into your browser:
                  </p>
                  <p style="margin:0 0 32px;font-size:12px;color:#2E75B6;word-break:break-all;">
                    {confirmation_url}
                  </p>

                  <hr style="border:none;border-top:1px solid #eeeeee;margin:0 0 24px;">

                  <p style="margin:0;font-size:13px;color:#aaaaaa;line-height:1.6;">
                    If you weren't expecting this email, you can safely ignore it —
                    your membership will remain inactive until the link above is used.
                  </p>
                </td>
              </tr>

              <!-- Footer -->
              <tr>
                <td style="background-color:#f9f9f9;padding:20px 40px;border-top:1px solid #eeeeee;">
                  <p style="margin:0;font-size:12px;color:#aaaaaa;text-align:center;">
                    This email was sent by {library_name} via the Library Management System.
                    Please do not reply to this email.
                  </p>
                </td>
              </tr>

            </table>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """


def _build_active_email_html(member_name: str, library_name: str, membership_no: str) -> str:
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="margin:0;padding:0;background-color:#f4f6f8;font-family:Arial,sans-serif;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f6f8;padding:40px 0;">
        <tr>
          <td align="center">
            <table width="560" cellpadding="0" cellspacing="0"
                   style="background:#ffffff;border-radius:8px;overflow:hidden;
                          box-shadow:0 1px 4px rgba(0,0,0,0.08);">

              <!-- Header -->
              <tr>
                <td style="background-color:#1E3A5F;padding:32px 40px;">
                  <p style="margin:0;font-size:20px;font-weight:bold;color:#ffffff;">
                    Your membership is active
                  </p>
                </td>
              </tr>

              <!-- Body -->
              <tr>
                <td style="padding:40px;">
                  <p style="margin:0 0 16px;font-size:16px;color:#1a1a1a;">
                    Hi {member_name},
                  </p>
                  <p style="margin:0 0 24px;font-size:15px;color:#444444;line-height:1.6;">
                    Your email address has been confirmed and your membership
                    at <strong>{library_name}</strong> is now
                    <strong>active</strong>. Your membership number is {membership_no} You're all set to start borrowing
                    books and using the library's services.
                  </p>

                  <hr style="border:none;border-top:1px solid #eeeeee;margin:0 0 24px;">

                  <p style="margin:0;font-size:13px;color:#aaaaaa;line-height:1.6;">
                    If you didn't request this, please get in touch with
                    {library_name} directly.
                  </p>
                </td>
              </tr>

              <!-- Footer -->
              <tr>
                <td style="background-color:#f9f9f9;padding:20px 40px;border-top:1px solid #eeeeee;">
                  <p style="margin:0;font-size:12px;color:#aaaaaa;text-align:center;">
                    This email was sent by {library_name} via the Library Management System.
                    Please do not reply to this email.
                  </p>
                </td>
              </tr>

            </table>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """