"""
emails.py -- MEMBERS module

The actual "talk to Resend and send an HTML email" logic for the members
app. This module does NOT get called directly from views.py anymore —
it's called from apps/members/tasks.py, which runs on a Celery worker.
See tasks.py for why (short version: sending email is slow-ish and can
fail/need retrying, and we don't want an HTTP request that creates a
member to sit there waiting on a third-party API call).

Two emails live here:

    1. send_member_welcome_email()  -> queued the moment a member is
       created. Tells them which library added them and asks them to
       confirm their email address before their membership becomes active.

    2. send_member_active_email()   -> queued right after the member
       clicks the confirmation link and their status flips inactive ->
       active.

We use the `resend` Python SDK directly, mirroring the exact same pattern
already used in apps/users/emails.py, so anyone reading both files sees
the same shape and doesn't have to learn two different ways of sending
mail in this codebase.
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
) -> None:
    """
    Sends the "you've been added as a member" + "please confirm your
    email" message.

    NOTE ON ERROR HANDLING: this function deliberately does NOT catch
    exceptions from resend.Emails.send() — it lets them propagate. That's
    intentional now that this is called from inside a Celery task (see
    apps/members/tasks.py::send_member_welcome_email_task), which is what
    actually catches the exception, logs it, and retries the task a few
    times with a delay. If we swallowed the error here, the task would
    always look "successful" to Celery even when the email genuinely
    failed to send, and it would never retry.

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
    """
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


def send_member_active_email(
    member_name: str,
    library_name: str,
    to_email: str,
) -> None:
    """
    Sends the "your account is now active" confirmation.

    Same "let exceptions propagate" reasoning as
    send_member_welcome_email() above — apps/members/tasks.py is what
    catches failures here and drives the retry.
    """
    resend.Emails.send(
        {
            "from": settings.RESEND_FROM_EMAIL,
            "to": [to_email],
            "subject": f"Your {library_name} membership is now active",
            "html": _build_active_email_html(member_name, library_name),
        }
    )


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


def _build_active_email_html(member_name: str, library_name: str) -> str:
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
                    <strong>active</strong>. You're all set to start borrowing
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