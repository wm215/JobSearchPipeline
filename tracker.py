#!/usr/bin/env python3
"""
Job Tracker V2 - ACCURATE EXTRACTION

Key improvements:
1. Person name blacklist (rejects William Melendez, etc.)
2. Email address validation (rejects @xxx.com as companies)
3. Company name quality checks
4. Position title validation
5. Conservative status detection
6. Detailed logging for debugging
"""

import os
import re
import pickle
from datetime import datetime, timedelta
import sys
import json
from dotenv import load_dotenv

# Load environment variables
load_dotenv(os.path.expanduser('~/.env'))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sheets_helper import get_sheets_service, get_spreadsheet_id

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    import base64
except ImportError:
    print("ERROR: Google API libraries not installed.")
    exit(1)

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    print("⚠️  OpenAI not available, using regex fallback")

GMAIL_SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify'
]

# VALIDATION LISTS
PERSON_NAMES = [
    'william melendez', 'wiliam melendez', 'william anthony melendez',
    'jessica haralson-askew', 'jessica negley', 'sharayil ignatius',
    'carie brannam', 'aaron lawlor', 'alyssa schaal', 'stephen st.vincent',
    'curry, michael j', 'kay pugh', 'lanelle lanton', 'gary parker',
    'joyce hill', 'lindsey joyce', 'human resources', 'recruiting team',
    'talent team', 'hiring manager', 'hr department', 'careers team',
    # New person names from cleanup
    'dan lustig', 'dan leader', 'almodovar, veronica', 'veronica almodovar'
]

GARBAGE_COMPANIES = [
    'invalidemail', 'do not reply', 'the', 'thecha', 'this time',
    'talent acquisition specialist', 'careers netflix', 'cha careers',
    'governmentjobs', 'careers', 'fbi', 'pha', 'department of',
    'state of', 'city of', 'county of', 'office of',
    'the lime recruiting team', 'recruiting team', 'hr team',
    # New garbage patterns from cleanup
    'hud and recently', 'amtrak talent acquisition', 'and recently',
    # Non-job companies (travel, bills, etc.) - Feb 2026 cleanup
    'mail delivery subsystem', 't-mobile', 'verizon', 'at&t wireless',
    'american airlines', 'alaska airlines', 'united airlines', 'delta air',
    'southwest airlines', 'spirit airlines', 'frontier airlines',
    'donotreply@nbis.mil', 'nbis.mil',  # Security clearance forms
    'jobs @ pepsi', 'jobs@pepsi',  # Job alerts, not confirmations
    'habitat',  # Too generic when position = company
    'livengrin foundation',  # Matched incorrectly
    'out professionals',  # Matched incorrectly
    'lanelle lanton',  # Person name that slipped through
]

# Non-job senders to completely skip
NON_JOB_SENDERS = [
    'mail delivery subsystem',
    't-mobile',
    'verizon wireless',
    'at&t',
    '@aa.com',  # American Airlines transactional
    '@alaskaair.com',  # Alaska Airlines transactional
    '@delta.com',
    '@southwest.com',
    '@spirit.com',
    '@nbis.mil',  # Security clearance
    'noreply@uber.com',
    'noreply@lyft.com',
    '@anixter.org',  # Employment services meeting, not a job application
]

ATS_SYSTEMS = [
    'successfactors', 'ashbyhq', 'greenhouse', 'lever', 'workday',
    'myworkday', 'icims', 'taleo', 'smartrecruiters', 'jobvite',
    'ultipro', 'bamboohr', 'paylocity', 'adp', 'namely', 'linkedin',
    'indeed', 'glassdoor', 'ziprecruiter', 'noreply', 'no-reply',
    'brassring', 'salesforce'
]

# QUICK WIN #2: Company name normalization table
COMPANY_NORMALIZATION = {
    # Philadelphia variations
    'city of philly': 'City of Philadelphia',
    'city of philadephia': 'City of Philadelphia',
    'philly': 'City of Philadelphia',
    'philadelphia': 'City of Philadelphia',
    'city of philadelphia hiring team': 'City of Philadelphia',

    # Chicago variations
    'city of chi': 'City of Chicago',
    'chicago': 'City of Chicago',

    # State variations
    'state of il': 'State of Illinois',
    'il state': 'State of Illinois',
    'illinois': 'State of Illinois',
    'state of illinois': 'State of Illinois',

    # Federal agencies
    'hud': 'U.S. Department of Housing and Urban Development',
    'u.s. hud': 'U.S. Department of Housing and Urban Development',
    'dept of hud': 'U.S. Department of Housing and Urban Development',
    'omb': 'Office of Management and Budget',
    'eop-omb': 'Office of Management and Budget',
    'gsa': 'General Services Administration',
    'epa': 'Environmental Protection Agency',

    # Housing authorities
    'cha': 'Chicago Housing Authority',
    'thecha': 'Chicago Housing Authority',
    'pha': 'Philadelphia Housing Authority',

    # GSEs
    'fnma': 'Fannie Mae',
    'fannie': 'Fannie Mae',
    'freddie': 'Freddie Mac',
    'fhlmc': 'Freddie Mac',

    # Cook County
    'cook county': 'Cook County Government',
    'cook county government': 'Cook County Government',
    'clerk of the circuit court': 'Cook County - Clerk of Circuit Court',

    # Common variations
    'state of de': 'State of Delaware',
    'delaware': 'State of Delaware',
    'septa': 'SEPTA',
    'amtrak': 'Amtrak',

    # Clean up hiring team suffixes
    'monarch money hiring team': 'Monarch Money',
    'equip health hiring team': 'Equip Health',
    'hims & hers talent acquisition no r': 'Hims & Hers',
}

INVALID_POSITION_KEYWORDS = [
    'your', 'completed', 'or it could be', 'working',
    'response to job inquiry', 'new jobs from', 'job posting notification',
    'seeking guidance', 'from alaska airlines',
    'thank you', 'thanks', 'interview self', 'webex link', 'zoom link',
    'teams link', 'automatic reply', 'auto-reply', 'out of office',
    'connecting re:', 'following up', 'quick question', 'checking in',
    # New patterns found from cleanup
    'your recent job', 'your job', 'your job application', 'following', 'application',
    'job application submitted', 'director au', 'exploring new opportunities',
    'draft job description', 'your recent job application',
    # Feb 2026 cleanup - non-job patterns
    'applying',  # Too generic
    'receipt', 'your bill', 'bill payment', 'payment request',
    'trip confirmation', 'flight confirmation', 'your trip',
    'reservation', 'canceled reservation', 'booking confirmation',
    'delivery status', 'delivery notification',
    'sf85p', 'please make correction',  # Security clearance forms
    'new jobs from',  # Job alerts, not applications
    "we've received your application",  # Too generic
    'william: jobs',  # Generic job alert
]

class AccurateJobTracker:
    def __init__(self):
        self.gmail_service = None
        self.sheets_service = None
        self.spreadsheet_id = None
        self.applications = {}
        self.skipped_count = 0
        self.skip_reasons = []
        self.ai_client = None
        self.ai_extractions = 0
        self.regex_extractions = 0
        self.ai_disabled_reason = None
        
        # Initialize OpenAI client if available
        if OPENAI_AVAILABLE:
            api_key = os.getenv('OPENAI_API_KEY')
            if api_key:
                self.ai_client = OpenAI(api_key=api_key)
                print("✅ OpenAI client initialized")
            else:
                print("⚠️  OPENAI_API_KEY not found in environment")

    def authenticate_gmail(self):
        """Authenticate with Gmail API"""
        creds = None
        token_path = os.path.expanduser('~/gmail_token.pickle')
        credentials_path = os.path.expanduser('~/gmail_credentials.json')

        if os.path.exists(token_path):
            with open(token_path, 'rb') as token:
                creds = pickle.load(token)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    with open(token_path, 'wb') as token:
                        pickle.dump(creds, token)
                except Exception as e:
                    print(f"⚠️  Token refresh failed ({e}), re-authenticating...")
                    creds = None

            if not creds or not creds.valid:
                from google_auth_oauthlib.flow import InstalledAppFlow
                flow = InstalledAppFlow.from_client_secrets_file(credentials_path, GMAIL_SCOPES)
                creds = flow.run_local_server(port=0)
                with open(token_path, 'wb') as token:
                    pickle.dump(creds, token)

        self.gmail_service = build('gmail', 'v1', credentials=creds)
        print("✓ Gmail authenticated")

    def authenticate_sheets(self):
        """Authenticate with Google Sheets"""
        self.sheets_service = get_sheets_service()
        self.spreadsheet_id = get_spreadsheet_id()
        print("✓ Google Sheets authenticated")

    def normalize_company_name(self, company):
        """QUICK WIN #2: Normalize company name variations to canonical names"""
        if not company:
            return company

        company_lower = company.lower().strip()

        # Check exact match first
        if company_lower in COMPANY_NORMALIZATION:
            return COMPANY_NORMALIZATION[company_lower]

        # Check partial matches for common patterns
        for variant, canonical in COMPANY_NORMALIZATION.items():
            if variant in company_lower:
                return canonical

        # Clean up common suffixes
        company = re.sub(r'\s+(hiring team|recruiting team|talent team|careers|hr)$', '', company, flags=re.IGNORECASE).strip()

        return company

    def get_email_body(self, message):
        """Extract full email body"""
        try:
            if 'payload' not in message:
                return ''

            payload = message['payload']
            body = ''

            if 'body' in payload and 'data' in payload['body']:
                body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='ignore')

            elif 'parts' in payload:
                for part in payload['parts']:
                    if part.get('mimeType') == 'text/plain':
                        if 'data' in part['body']:
                            body += base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')
                    elif part.get('mimeType') == 'text/html':
                        if 'data' in part['body']:
                            html_body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')
                            body += re.sub(r'<[^>]+>', ' ', html_body)

            return body
        except Exception as e:
            return ''

    def is_valid_company_name(self, company):
        """Validate company name quality"""
        if not company or len(company) < 2:
            return False, "Too short"

        company_lower = company.lower().strip()

        # Check person names
        if any(name in company_lower for name in PERSON_NAMES):
            return False, f"Person name: {company}"

        # Check garbage companies (exact match)
        if company_lower in GARBAGE_COMPANIES:
            return False, f"Garbage company: {company}"

        # Check for incomplete/truncated names
        incomplete_patterns = [
            'department of$', 'state of$', 'city of$', 'county of$', 'office of$',
            'careers at ', 'jobs at ', 'apply to ', 'working at '
        ]
        for pattern in incomplete_patterns:
            if re.search(pattern, company_lower):
                return False, f"Incomplete name: {company}"

        # Reject names that end with "and wish you" or similar rejection email fragments
        if 'wish you' in company_lower or 'best of luck' in company_lower:
            return False, f"Email fragment: {company}"

        # Reject partial sentence patterns (but allow "and" in legitimate names like "Office of Management and Budget")
        partial_sentence_patterns = [' and recently', ' and appreciate', ' and are ', ' and wish']
        if any(pattern in company_lower for pattern in partial_sentence_patterns):
            return False, f"Partial sentence: {company}"

        # Check for email addresses
        if '@' in company and '.com' in company:
            return False, f"Email address: {company}"

        # Check for ATS systems
        if company_lower in ATS_SYSTEMS:
            return False, f"ATS system: {company}"

        # Check for suspiciously short names (except valid ones)
        if len(company_lower) <= 3 and company_lower not in ['hud', 'irs', 'fbi', 'epa', 'dot', 'gsa']:
            return False, f"Too short: {company}"

        # Check for email-like patterns
        if company_lower.endswith('.com') or company_lower.endswith('.gov') or company_lower.endswith('.org'):
            return False, f"Domain name: {company}"

        # Check for "quoted" names (usually from email headers)
        if company.startswith('"') and company.endswith('"'):
            return False, f"Quoted name: {company}"

        # Reject if it looks like a person name (First Last pattern with common first names)
        common_first_names = ['john', 'jane', 'mike', 'michael', 'david', 'sarah', 'james',
                              'robert', 'jennifer', 'linda', 'mary', 'patricia', 'elizabeth',
                              'joyce', 'lindsey', 'ashley', 'amanda', 'jessica', 'stephanie']
        words = company_lower.split()
        if len(words) == 2 and words[0] in common_first_names:
            return False, f"Likely person name: {company}"

        return True, "Valid"

    def is_valid_position_title(self, position):
        """Validate position title quality"""
        if not position or len(position) < 3:
            return False, "Too short"

        position_lower = position.lower().strip()

        # Check for invalid keywords (exact match)
        if position_lower in INVALID_POSITION_KEYWORDS:
            return False, f"Invalid keyword: {position}"

        # Check for invalid keywords (partial match)
        invalid_partials = [
            'thank you', 'thanks for', 'interview self', 'webex link', 'zoom link',
            'teams link', 'automatic reply', 'auto-reply', 'out of office',
            'connecting re:', 'following up', 'quick question', 'checking in',
            'calendar invite', 'meeting invite', 'your upcoming', 'reminder:',
            're:', 'fwd:', 'fw:',
            # New patterns from cleanup
            'interview w ', 'interview with ', 'and are delighted', 'and appreciate',
            'time to apply for', 'job was', 'filling of vacancies', 'our director',
            'exploring new opportunit', 'job application submitted',
            # Feb 2026 cleanup - travel/bill patterns
            'trip confirmation', 'flight confirmation', 'booking confirmation',
            'your bill', 'bill payment', 'payment request', 'payment confirmation',
            'delivery status', 'delivery notification', 'shipping update',
            'canceled reservation', 'your reservation',
            'sf85p', 'please make correction', 'review and follow-up',
        ]
        if any(invalid in position_lower for invalid in invalid_partials):
            return False, f"Email subject pattern: {position}"

        # Check if it's just "director" or generic single words
        if position_lower in ['director', 'manager', 'analyst', 'your', 'completed', 'working']:
            return False, f"Too generic: {position}"

        # Reject positions that look like dates/times (e.g., "Friday, August 28, 3:30 PM")
        if re.search(r'(monday|tuesday|wednesday|thursday|friday|saturday|sunday)', position_lower):
            if re.search(r'\d{1,2}:\d{2}|\d{1,2}\s*(am|pm)', position_lower):
                return False, f"Date/time pattern: {position}"

        # Reject positions with [BRACKETS] that look like email tags
        if re.search(r'\[.*?(WEBMAIL|PERSONNEL|AUTO|EXTERNAL|INTERNAL)\]', position, re.IGNORECASE):
            return False, f"Email tag pattern: {position}"

        # Reject location patterns like "Philadelphia, City of (PA)" or "Chicago, IL"
        if re.search(r'\([A-Z]{2}\)\s*$', position) or re.search(r',\s*[A-Z]{2}\s*$', position):
            return False, "Location pattern"

        # Check for HTML artifacts
        if '<br' in position or '&amp;' in position or '&nbsp;' in position:
            # Clean it first
            position = re.sub(r'<[^>]+>', '', position)
            position = position.replace('&amp;', '&').replace('&nbsp;', ' ').strip()
            if len(position) < 3:
                return False, "HTML artifact"

        return True, "Valid"
    
    def extract_with_ai(self, subject, body, sender):
        """
        Use OpenAI to extract job application details
        Returns: dict with company, position, application_date, confidence
        """
        if not self.ai_client:
            return None
        
        try:
            # Truncate body to first 2000 chars to save tokens
            body_snippet = body[:2000] if body else ""
            
            prompt = f"""Extract job application details from this email.

Subject: {subject}
From: {sender}
Body: {body_snippet}

Return ONLY a JSON object with these fields:
- company: The company name (normalized, e.g., "City of Philadelphia" not "City of Philly")
- position: The job title/position applied for
- application_date: The date in YYYY-MM-DD format (or null if not found)
- confidence: "high", "medium", or "low"

If this is NOT a job application email, return {{"is_job_application": false}}

Rules:
- Normalize company names (State of IL → State of Illinois)
- Reject person names as companies (e.g., Dan Lustig, Joyce Hill)
- Clean position titles (remove "the", "a", etc.)
- Return null for any field you're unsure about"""

            response = self.ai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a data extraction assistant. Return only valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0,
                max_tokens=200
            )
            
            result_text = response.choices[0].message.content.strip()
            
            # Parse JSON response
            # Remove markdown code blocks if present
            if result_text.startswith('```'):
                result_text = re.sub(r'```json\n|\n```|```', '', result_text).strip()
            
            result = json.loads(result_text)
            
            # Check if it's a job application
            if result.get('is_job_application') == False:
                return {'is_job_application': False}
            
            self.ai_extractions += 1
            return result
            
        except Exception as e:
            err = str(e)
            if 'insufficient_quota' in err or '429' in err:
                self.ai_disabled_reason = "OpenAI quota exceeded"
                self.ai_client = None
                print("⚠️  AI extraction disabled for this run (quota exceeded); using regex fallback only.")
            else:
                print(f"⚠️  AI extraction failed: {e}")
            return None

    def extract_company_from_email(self, subject, body, sender):
        """
        Extract company name with STRICT validation
        Returns: (company_name, confidence_level, extraction_source)
        """

        # Priority 0: LinkedIn Easy Apply pattern
        # "Your application to [Position] at [Company]"
        if 'linkedin' in sender.lower():
            linkedin_match = re.search(r'Your application to .+ at (.+?)(?:\s+was|\s*$)', subject)
            if linkedin_match:
                company = linkedin_match.group(1).strip()
                return company, 'high', 'linkedin_subject'

        # Priority 0.3: Workday multi-tenant format — local-part identifies the company
        # e.g. luriechildrens@myworkday.com → Lurie Children's Hospital
        if 'myworkday.com' in sender.lower() or '@workday.com' in sender.lower():
            email_match = re.search(r'[\w\.-]+@[\w\.-]+', sender)
            if email_match:
                local_part = email_match.group(0).split('@')[0].lower()
                workday_company_map = {
                    'luriechildrens': "Lurie Children's Hospital",
                    'cityofchicago': 'City of Chicago',
                    'cookcountyil': 'Cook County Government',
                    'cookcounty': 'Cook County Government',
                    'phila': 'City of Philadelphia',
                    'fanniemae': 'Fannie Mae',
                    'freddiemac': 'Freddie Mac',
                    'amtrak': 'Amtrak',
                    'thecha': 'Chicago Housing Authority',
                }
                if local_part in workday_company_map:
                    return workday_company_map[local_part], 'high', 'workday_local_part'
                # Generic fallback: convert camelCase/run-on to title case
                company_guess = re.sub(r'([a-z])([A-Z])', r'\1 \2', local_part).title()
                if len(company_guess) > 3:
                    return company_guess, 'medium', 'workday_local_part'

        # Priority 0.4: GovernmentJobs.com — company name is in the subject
        # e.g. "Application received by Philadelphia, City of (PA)"
        if 'governmentjobs.com' in sender.lower():
            gov_match = re.search(r'(?:received by|Application for|from)\s+(.+?)(?:\s*[\(\|]|,\s*[A-Z]{2}\b|$)', subject, re.IGNORECASE)
            if gov_match:
                company = gov_match.group(1).strip()
                # Normalize "Philadelphia, City of" → "City of Philadelphia"
                inversion_match = re.match(r'^(.+?),\s+(City|County|State|Town|Village|Township) of$', company, re.IGNORECASE)
                if inversion_match:
                    company = f"{inversion_match.group(2)} of {inversion_match.group(1)}"
                company = self.normalize_company_name(company)
                is_valid, _ = self.is_valid_company_name(company)
                if is_valid:
                    return company, 'high', 'governmentjobs_subject'

        # Priority 0.45: SmartRecruiters — sender name is "Person from Company <email>"
        if 'smartrecruiters.com' in sender.lower():
            from_match = re.search(r'from\s+([A-Z][A-Za-z\s&\.,\-]+?)(?:\s*<|$)', sender)
            if from_match:
                company = from_match.group(1).strip()
                company = self.normalize_company_name(company)
                is_valid, _ = self.is_valid_company_name(company)
                if is_valid:
                    return company, 'high', 'smartrecruiters_sender'

        # Priority 0.5: Check for USAJobs pattern in subject
        # "Application for Position, EOP-OMB-123456"
        usajobs_pattern = r',\s+([\w-]+)-\d{8}'
        usajobs_match = re.search(usajobs_pattern, subject)
        if usajobs_match and 'usastaffing' in sender.lower():
            agency_code = usajobs_match.group(1)
            # Map agency codes to names
            agency_map = {
                'EOP-OMB': 'Office of Management and Budget',
                'HUD': 'U.S. Department of Housing and Urban Development',
                'DOT': 'U.S. Department of Transportation',
                'DOL': 'U.S. Department of Labor',
                'EPA': 'Environmental Protection Agency',
                'GSA': 'General Services Administration',
            }
            if agency_code in agency_map:
                return agency_map[agency_code], 'high', 'usajobs_subject'

        # Priority 0.7: Cook County HR uses a broken domain — detect via sender name
        if 'invalidemail.com' in sender.lower() or 'hr-cookcountyil' in sender.lower():
            return 'Cook County Government', 'high', 'sender_name'

        # Priority 1: Check for known government/company domains in sender
        if '@' in sender:
            domain = sender.split('@')[1].lower()

            # Direct government domains
            if 'septa.org' in domain:
                return 'SEPTA', 'high', 'sender_domain'
            elif 'phila.gov' in domain or 'philly' in domain:
                return 'City of Philadelphia', 'high', 'sender_domain'
            elif 'cityofchicago' in domain or ('chicago' in domain and 'gov' in domain):
                return 'City of Chicago', 'high', 'sender_domain'
            elif 'cookcounty' in domain or 'cook-county' in domain:
                return 'Cook County', 'high', 'sender_domain'
            elif 'amtrak.com' in domain:
                return 'Amtrak', 'high', 'sender_domain'
            elif 'delaware.gov' in domain or 'state.de.us' in domain:
                return 'State of Delaware', 'high', 'sender_domain'
            elif 'illinois.gov' in domain or 'illinois2' in domain:
                return 'State of Illinois', 'high', 'sender_domain'
            elif 'fanniemae.com' in domain:
                return 'Fannie Mae', 'high', 'sender_domain'
            elif 'freddiemac.com' in domain:
                return 'Freddie Mac', 'high', 'sender_domain'
            elif 'pha.phila.gov' in domain:
                return 'Philadelphia Housing Authority', 'high', 'sender_domain'
            elif 'thecha.org' in domain:
                return 'Chicago Housing Authority', 'high', 'sender_domain'

        # Priority 2: Extract from subject line
        # Pattern: "Thank you for your application! - Company Name"
        subject_patterns = [
            r'(?:application|interest in|applying to)\s+([A-Z][A-Za-z\s&\.,\-]+?)(?:\s*-|\s*\||\s+position|$)',
            r'from\s+([A-Z][A-Za-z\s&\.,\-]+?)\s*-',
        ]

        for pattern in subject_patterns:
            matches = re.findall(pattern, subject)
            for match in matches:
                company = match.strip()
                # Clean up
                company = re.sub(r'\s+(team|careers|hiring|recruitment)$', '', company, flags=re.IGNORECASE).strip()

                is_valid, reason = self.is_valid_company_name(company)
                if is_valid and len(company) > 3:
                    return company, 'medium', 'subject_line'

        # Priority 3: Extract from email body
        body_text = subject + ' ' + body

        # Look for "position at [Company]" or "role with [Company]"
        body_patterns = [
            r'(?:position|role|opportunity|application)\s+(?:at|with)\s+(?:the\s+)?([A-Z][A-Za-z\s&\.,\-]+?)(?:\s+for|\s+as|\s+in|\.|\n|,)',
            r'(?:on behalf of|representing)\s+(?:the\s+)?([A-Z][A-Za-z\s&\.,\-]+?)(?:\.|,|\n)',
            r'([A-Z][A-Za-z\s&\.,\-]+?)\s+(?:is hiring|is seeking|is recruiting)',
            r'thank you for (?:your interest in|applying to)\s+(?:the\s+)?([A-Z][A-Za-z\s&\.,\-]+?)(?:\.|,|\n)',
            r'your application to\s+([A-Z][A-Za-z\s&\.,\-]+?)(?:\s+for|\.|,)',  # USAJobs pattern
            r'application for\s+[^,]+,\s+([A-Z][A-Za-z\s&\.,\-]+?)-\d{8}',  # "Application for Position, Company-12345"
        ]

        for pattern in body_patterns:
            matches = re.findall(pattern, body_text[:3000])  # Search first 3000 chars
            for match in matches:
                company = match.strip()
                # Clean up
                company = re.sub(r'\s+(team|careers|hiring|recruitment|talent|position|role|was|received)$', '', company, flags=re.IGNORECASE).strip()

                # Special handling for known entities
                if 'clerk of the circuit court' in company.lower():
                    return 'Cook County - Clerk of Circuit Court', 'high', 'body_text'
                if company.lower() == 'philadelphia':
                    return 'City of Philadelphia', 'high', 'body_text'
                if company.lower() == 'illinois':
                    return 'State of Illinois', 'high', 'body_text'
                if 'office of management and budget' in company.lower() or company.lower() == 'omb':
                    return 'Office of Management and Budget', 'high', 'body_text'

                is_valid, reason = self.is_valid_company_name(company)
                if is_valid and len(company) > 3:
                    return company, 'medium', 'body_text'

        # Priority 4: Try to extract from sender name (before email)
        # "Information Systems & Networks Corporation <email@adp.com>"
        sender_name_pattern = r'^(.+?)\s*<'
        sender_match = re.search(sender_name_pattern, sender)
        if sender_match:
            sender_name = sender_match.group(1).strip()
            # Remove quotes
            sender_name = sender_name.replace('"', '').strip()

            # Check if it's a valid company (not person name, not ATS)
            is_valid, reason = self.is_valid_company_name(sender_name)
            if is_valid and len(sender_name) > 5:
                # Clean up "InformationSystemsNetworksCorporation" → "Information Systems Networks Corporation"
                if not ' ' in sender_name and len(sender_name) > 20:
                    # CamelCase split
                    sender_name = re.sub(r'([a-z])([A-Z])', r'\1 \2', sender_name)

                return sender_name, 'low', 'sender_name'

        # If all else fails, return None
        return None, 'none', 'failed'

    def extract_position_from_email(self, subject, body):
        """
        Extract position title with validation
        """

        # LinkedIn Easy Apply pattern: "Your application to [Position] at [Company]"
        linkedin_match = re.search(r'Your application to (.+?) at .+', subject)
        if linkedin_match:
            position = linkedin_match.group(1).strip()
            is_valid, _ = self.is_valid_position_title(position)
            if is_valid:
                return position

        # City of Chicago pattern: "Your recent job application for [Position]"
        chicago_match = re.search(r'Your (?:recent )?job application for (.+?)(?:\s*$|\s+at\s+)', subject, re.IGNORECASE)
        if chicago_match:
            position = chicago_match.group(1).strip()
            is_valid, _ = self.is_valid_position_title(position)
            if is_valid:
                return position

        # Remove common prefixes
        subject_clean = re.sub(r'^(re:|fwd:|thank you for your application!?|thank you for applying for the|thank you for applying for|your application|application received for|application received by|application received|application for|update from)\s*-?\s*', '', subject, flags=re.IGNORECASE).strip()
        # Remove leading single-word "Company - " prefix (e.g. "Lime - Senior Regional Lead")
        # Only strips if a single word precedes the dash — avoids mangling "Position Title - Qualifier"
        subject_clean = re.sub(r'^\w+\s+-\s+(?=[A-Z])', '', subject_clean).strip() if ' - ' in subject_clean else subject_clean

        # Pattern 1: Subject often has "Position Title, JC #123" or "Position Title - Company"
        # Or "Position Title Employment Application Received"
        subject_patterns = [
            r'^([A-Z][A-Za-z\s,\-&/()]+?),\s+JC\s+#',  # "Inspector General Investigator, JC #9581"
            r'^([A-Z][A-Za-z\s,\-&/()]+?)\s+Employment Application',  # "Management Analyst III Employment Application"
            r'^([A-Z][A-Za-z\s,\-&/()]+?)\s+(?:at|with|for|−|\||$)',  # "Senior Policy Analyst at Company"
            r'^Application for\s+([A-Z][A-Za-z\s,\-&/()]+?)(?:,|$)',  # "Application for Policy Analyst"
            r'^([A-Z][A-Za-z\s,\-&/()]{5,}?)(?:\s*-\s*|\s+at\s+)',  # "Position Title - Company"
        ]

        for pattern in subject_patterns:
            matches = re.findall(pattern, subject_clean)
            for match in matches:
                position = match.strip()
                # Clean up trailing words
                position = re.sub(r'\s+(employment application received|application|position|role|opportunity|at|with|for|interview)$', '', position, flags=re.IGNORECASE).strip()
                # Remove job codes
                position = re.sub(r'\s*-\s*\d{8}$', '', position).strip()
                # Remove leading agency acronyms like "DCEO " before the real title
                position = re.sub(r'^[A-Z]{2,6}\s+(?=[A-Z])', '', position).strip()

                is_valid, reason = self.is_valid_position_title(position)
                if is_valid:
                    return position

        # QUICK WIN #1: Improved body text extraction
        # These patterns catch "Thank you for applying for the [POSITION]" emails
        body_patterns = [
            # "Thank you for applying for the X position"
            r'(?:thank you for applying|thanks for applying|application)\s+(?:for|to)\s+(?:the\s+)?([A-Z][A-Za-z\s,\-&/()0-9]+?)\s+(?:position|role|opportunity)',
            # "applying for the X position at Company"
            r'applying\s+(?:for\s+)?(?:the\s+)?([A-Z][A-Za-z\s,\-&/()0-9]+?)\s+(?:position|role)\s+(?:at|with)',
            # "interest in the X position"
            r'interest in the\s+([A-Z][A-Za-z\s,\-&/()0-9]+?)\s+(?:position|role)',
            # "applied for X position/role"
            r'applied for\s+(?:the\s+)?([A-Z][A-Za-z\s,\-&/()0-9]+?)\s+(?:position|role)',
            # "application to X position"
            r'application to\s+(?:the\s+)?([A-Z][A-Za-z\s,\-&/()0-9]+?)\s+(?:position|role)',
            # "Position: X" or "Role: X"
            r'(?:position|role|job title):\s*([A-Z][A-Za-z\s,\-&/()0-9]+?)(?:\n|<br|$|\.)',
            # "the X role at Company"
            r'the\s+([A-Z][A-Za-z\s,\-&/()0-9]+?)\s+(?:role|position)\s+at\s+',
            # "your application for X"
            r'your application for\s+(?:the\s+)?([A-Z][A-Za-z\s,\-&/()0-9]+?)(?:\s+position|\s+role|\s+at\s+|\.|\n)',
            # "for our X opening"
            r'for\s+(?:our|the)\s+([A-Z][A-Za-z\s,\-&/()0-9]+?)\s+(?:opening|vacancy|position)',
            # Howard Brown style: "Thank you for applying for the X position"
            r'Thank you for applying for the\s+([A-Z][A-Za-z\s,\-&/()0-9]+?)\s+position',
            # "regarding the X position"
            r'regarding\s+(?:the\s+)?([A-Z][A-Za-z\s,\-&/()0-9]+?)\s+(?:position|role|opening)',
            # Greenhouse/Lever style: "X at Company"
            r'^([A-Z][A-Za-z\s,\-&/()0-9]+?)\s+at\s+[A-Z]',
        ]

        for pattern in body_patterns:
            matches = re.findall(pattern, body[:4000], re.IGNORECASE)
            for match in matches:
                position = match.strip()
                # Clean HTML
                position = re.sub(r'<[^>]+>', '', position)
                position = position.replace('&amp;', '&').replace('&nbsp;', ' ').strip()
                # Remove trailing punctuation
                position = re.sub(r'[.,;:]+$', '', position).strip()
                # Capitalize properly
                if position.islower():
                    position = position.title()

                is_valid, reason = self.is_valid_position_title(position)
                if is_valid and len(position) > 5:
                    return position

        # Fallback: use cleaned subject if it looks valid and has enough content
        if len(subject_clean) > 10 and len(subject_clean) < 100:
            # Remove common suffixes
            subject_clean = re.sub(r'\s+(at|with|for)\s+.*$', '', subject_clean).strip()
            subject_clean = re.sub(r'\s+(position|role|opportunity)$', '', subject_clean, flags=re.IGNORECASE).strip()

            is_valid, reason = self.is_valid_position_title(subject_clean)
            if is_valid:
                return subject_clean

        return None

    def determine_status_from_email(self, subject, body):
        """
        CONSERVATIVE status detection
        Default to 'Applied' unless we have explicit evidence
        """
        content = (subject + ' ' + body).lower()

        # REJECTED - Must have EXPLICIT rejection language
        if any(phrase in content for phrase in [
            'not moving forward with your application',
            'not selected for',
            'not been selected',
            'position has been filled',
            'selected other candidates',
            'will not be moving forward',
            'decided to pursue other candidates',
            'regret to inform you that you were not',
            'regret to inform you that your application',
            'application was not selected',
            'we have filled the position',
            'chosen to move forward with other candidates'
        ]):
            return 'Rejected'

        # OFFER - Must have VERY specific offer language
        if any(phrase in content for phrase in [
            'pleased to extend an offer',
            'extend you an offer',
            'offering you the position',
            'we are offering you',
            'accept our offer',
            'offer of employment',
            'would like to offer you the position',
            'offer you the role'
        ]):
            return 'Offer'

        # INTERVIEW - Subject line with "interview" is a strong signal
        subject_lower = subject.lower()
        if 'interview' in subject_lower and not any(x in subject_lower for x in [
            'automatic reply', 'auto-reply', 'out of office', 'thank you'
        ]):
            return 'Interview'

        # INTERVIEW - Body scheduling/confirming language
        if any(phrase in content for phrase in [
            'interview is scheduled',
            'interview scheduled for',
            'your interview on',
            'your interview at',
            'zoom interview link',
            'teams interview link',
            'schedule your interview',
            'please confirm your interview',
            'looking forward to meeting you on',
            'looking forward to speaking with you on'
        ]):
            return 'Interview'

        # UNDER REVIEW - Only if explicitly stated
        if any(phrase in content for phrase in [
            'application is under review',
            'currently reviewing your application',
            'under consideration'
        ]):
            return 'Under Review'

        # DEFAULT - Applied
        return 'Applied'

    def extract_date_from_email(self, email_date_str):
        """Parse email date"""
        try:
            date = datetime.strptime(email_date_str, '%a, %d %b %Y %H:%M:%S %z')
            return date.strftime('%Y-%m-%d')
        except:
            try:
                date = datetime.strptime(email_date_str, '%a, %d %b %Y %H:%M:%S %Z')
                return date.strftime('%Y-%m-%d')
            except:
                return datetime.now().strftime('%Y-%m-%d')

    def extract_urls_from_body(self, body):
        """Extract URLs from email body"""
        url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
        urls = re.findall(url_pattern, body)

        # Filter out tracking/unsubscribe URLs
        filtered_urls = []
        for url in urls:
            if not any(x in url.lower() for x in ['unsubscribe', 'tracking', 'pixel', 'analytics', 'utm_']):
                filtered_urls.append(url)

        return filtered_urls[:3]  # Max 3 URLs

    def sweep_all_gmail(self, days_back=365):
        """
        Sweep ALL of Gmail (every folder and label) for job-related emails that
        haven't been tagged yet. Uses a two-pass approach:
          Pass 1: metadata-only fetch to quickly identify candidates by subject/sender
          Pass 2: full fetch only for confirmed candidates
        Tags found emails as Jobs/Active so scan_gmail_full() picks them up.
        """
        print("🔭 SWEEPING all Gmail for untagged job emails...")

        date_filter = f'after:{(datetime.now() - timedelta(days=days_back)).strftime("%Y/%m/%d")}'

        # All known job-related senders and subject patterns
        known_senders = [
            'linkedin.com', 'indeed.com', 'workday.com', 'myworkday.com',
            'greenhouse.io', 'lever.co', 'ashbyhq.com', 'icims.com',
            'taleo.net', 'successfactors.com', 'smartrecruiters.com',
            'jobvite.com', 'rippling.com', 'governmentjobs.com',
            'usastaffing.gov', 'usastaffingoffice.gov',
            'phila.gov', 'cityofchicago.org', 'thecha.org',
            'cookcounty.gov', 'delaware.gov', 'illinois.gov',
            'fanniemae.com', 'freddiemac.com', 'howardbrown.org',
            'luriechildrens.org', 'lifetime.life', 'lifetimefitness.com',
            'septa.org', 'amtrak.com', 'rutgers.edu',
        ]
        sender_query = ' OR '.join(f'from:{d}' for d in known_senders)
        subject_query = (
            'subject:"application received" OR subject:"thank you for applying" '
            'OR subject:"your application" OR subject:"employment application" '
            'OR subject:"application submitted" OR subject:"application confirmation" '
            'OR subject:"we received your application" OR subject:"application for"'
        )

        # Skip emails already processed or correctly categorized
        # Jobs/Alerts holds suggestion emails and must never be swept into the pipeline
        already_filed = (
            'NOT label:Jobs-Alerts NOT label:Jobs-Applied NOT label:Jobs-Interview '
            'NOT label:Jobs-Rejected NOT label:Jobs-Offer NOT label:Jobs-Under-Review '
            'NOT label:Jobs-Active'
        )

        query = f'({sender_query} OR {subject_query}) {already_filed} {date_filter}'

        try:
            results = self.gmail_service.users().messages().list(
                userId='me',
                q=query,
                maxResults=500
            ).execute()

            candidates = results.get('messages', [])
            print(f"   Found {len(candidates)} candidate emails across all Gmail\n")

            if not candidates:
                print("   No untagged job emails found.")
                return

            active_label_id = self.get_or_create_label('Jobs/Active')
            if not active_label_id:
                return

            tagged = 0
            skipped = 0
            for msg_ref in candidates:
                try:
                    # Pass 1: metadata only — check subject/sender before full fetch
                    msg = self.gmail_service.users().messages().get(
                        userId='me',
                        id=msg_ref['id'],
                        format='metadata',
                        metadataHeaders=['Subject', 'From']
                    ).execute()

                    current_labels = msg.get('labelIds', [])
                    if active_label_id in current_labels:
                        continue  # Already tagged

                    headers = {h['name']: h['value'] for h in msg['payload']['headers']}
                    subject = headers.get('Subject', '')
                    sender = headers.get('From', '')

                    # Skip self-sent and non-job senders
                    if 'wmelendez215@gmail.com' in sender:
                        continue
                    if any(s in sender.lower() for s in NON_JOB_SENDERS):
                        skipped += 1
                        continue

                    # Quick subject sanity check — skip obvious non-job emails
                    subject_lower = subject.lower()
                    skip_subjects = [
                        'payment', 'invoice', 'receipt', 'bill', 'booking',
                        'reservation', 'flight', 'hotel', 'delivery', 'shipment',
                        'massage', 'insurance', 'cleaning', 'reminder',
                    ]
                    if any(s in subject_lower for s in skip_subjects):
                        skipped += 1
                        continue

                    # Tag as Jobs/Active and archive from inbox
                    remove_ids = ['INBOX'] if 'INBOX' in current_labels else []
                    self.gmail_service.users().messages().modify(
                        userId='me',
                        id=msg_ref['id'],
                        body={'addLabelIds': [active_label_id], 'removeLabelIds': remove_ids}
                    ).execute()
                    tagged += 1

                except Exception:
                    continue  # Skip individual failures, don't crash the sweep

            print(f"   ✓ Tagged {tagged} new emails as Jobs/Active")
            print(f"   ✗ Skipped {skipped} non-job emails\n")

        except Exception as e:
            print(f"   ⚠️  Sweep error: {e}")

    def scan_gmail_full(self, days_back=365):
        """
        Full scan - process all emails and update Google Sheets
        """
        print(f"\n🔍 FULL SCAN - Scanning past {days_back} days...")
        print("="*80)

        date_filter = f'after:{(datetime.now() - timedelta(days=days_back)).strftime("%Y/%m/%d")}'
        # Jobs/Tracker now holds actual application emails (alerts moved to Jobs/Alerts)
        query = f'(label:Jobs OR label:Jobs-Active OR label:Jobs-Archive OR label:Jobs-Tracker) {date_filter}'

        try:
            results = self.gmail_service.users().messages().list(
                userId='me',
                q=query,
                maxResults=500
            ).execute()

            messages = results.get('messages', [])
            print(f"Found {len(messages)} emails in Jobs labels\n")

            applications = {}  # Keyed by (company, position)
            processed_msg_ids = []  # Track successfully processed emails for archiving
            msg_statuses = {}  # Track status per message ID for label routing
            skipped = 0

            for idx, msg_ref in enumerate(messages, 1):
                msg_id = msg_ref['id']
                try:
                    message = self.gmail_service.users().messages().get(
                        userId='me',
                        id=msg_id,
                        format='full'
                    ).execute()
                except Exception as fetch_err:
                    skipped += 1
                    if idx % 50 == 0:
                        print(f"   ⚠️  Skipped email {idx} (fetch error): {fetch_err}")
                    continue

                # Extract headers
                headers = message['payload']['headers']
                subject = ''
                sender = ''
                date_str = ''

                for header in headers:
                    if header['name'] == 'Subject':
                        subject = header['value']
                    elif header['name'] == 'From':
                        sender = header['value']
                    elif header['name'] == 'Date':
                        date_str = header['value']

                # Skip self-sent emails
                if 'wmelendez215@gmail.com' in sender:
                    skipped += 1
                    continue

                # Skip known non-job senders
                sender_lower = sender.lower()
                if any(s in sender_lower for s in NON_JOB_SENDERS):
                    skipped += 1
                    continue

                # Extract body
                body = self.get_email_body(message)

                # Try AI extraction first, fall back to regex
                ai_result = self.extract_with_ai(subject, body, sender)
                
                if ai_result and ai_result.get('is_job_application') != False:
                    # AI successfully extracted data
                    company = ai_result.get('company')
                    position = ai_result.get('position')
                    confidence = ai_result.get('confidence', 'medium')
                    source = 'openai'
                    
                    # Normalize company name
                    company = self.normalize_company_name(company) if company else None
                    
                    # Get status and date using existing methods
                    status = self.determine_status_from_email(subject, body)
                    date_applied = ai_result.get('application_date') or self.extract_date_from_email(date_str)
                    
                    print(f"   🤖 AI: {company} - {position} ({confidence})")
                else:
                    # Fall back to regex extraction
                    company, confidence, source = self.extract_company_from_email(subject, body, sender)
                    company = self.normalize_company_name(company)
                    position = self.extract_position_from_email(subject, body)
                    status = self.determine_status_from_email(subject, body)
                    date_applied = self.extract_date_from_email(date_str)
                    
                    self.regex_extractions += 1
                
                urls = self.extract_urls_from_body(body)

                # Extract contact email from sender
                contact_email = ''
                if '@' in sender:
                    email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', sender)
                    if email_match:
                        contact_email = email_match.group(0)

                # Validate
                if company:
                    is_valid_co, co_reason = self.is_valid_company_name(company)
                    if not is_valid_co:
                        company = None
                        self.skip_reasons.append(f"Row {idx}: {co_reason}")

                if position:
                    is_valid_pos, pos_reason = self.is_valid_position_title(position)
                    if not is_valid_pos:
                        position = None
                        self.skip_reasons.append(f"Row {idx}: {pos_reason}")

                # Only add if valid
                if company and position:
                    key = (company, position)
                    processed_msg_ids.append(msg_id)  # Track for archiving
                    msg_statuses[msg_id] = status  # Track status for label routing

                    # Deduplicate - keep most recent or most specific status
                    if key in applications:
                        existing = applications[key]
                        # Update status if this is more specific
                        if status in ['Interview', 'Rejected', 'Offer']:
                            existing['status'] = status
                        if urls and not existing.get('urls'):
                            existing['urls'] = urls
                        if contact_email and not existing.get('contact_email'):
                            existing['contact_email'] = contact_email
                    else:
                        applications[key] = {
                            'company': company,
                            'position': position,
                            'status': status,
                            'date_applied': date_applied,
                            'contact_email': contact_email,
                            'urls': urls,
                            'last_updated': datetime.now().strftime('%Y-%m-%d')
                        }

                    if idx % 50 == 0:
                        print(f"Processed {idx}/{len(messages)}...")
                else:
                    skipped += 1

            print(f"\n✅ Extracted {len(applications)} valid applications")
            print(f"⚠️  Skipped {skipped} emails (invalid data)")
            
            # Show AI vs Regex stats
            if self.ai_client:
                total_extractions = self.ai_extractions + self.regex_extractions
                if total_extractions > 0:
                    ai_pct = (self.ai_extractions / total_extractions * 100)
                    print(f"\n📊 Extraction Methods:")
                    print(f"   🤖 AI: {self.ai_extractions} ({ai_pct:.1f}%)")
                    print(f"   📝 Regex: {self.regex_extractions} ({100-ai_pct:.1f}%)")
            print()

            # Now update Google Sheets
            self.update_google_sheets(applications)

            # Move processed emails to correct status labels
            self.ensure_status_labels_exist()
            self.label_emails_by_status(processed_msg_ids, msg_statuses)

        except Exception as e:
            print(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()

    def update_google_sheets(self, applications):
        """Update Google Sheets with applications"""
        print("📝 Updating Google Sheets...")

        # Load existing apps
        result = self.sheets_service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range='Applications!A:I'
        ).execute()

        existing_rows = result.get('values', [])
        headers = existing_rows[0] if existing_rows else []

        # Find existing applications
        existing_keys = {}
        for idx, row in enumerate(existing_rows[1:], start=2):
            if len(row) > 2:
                company = row[1] if len(row) > 1 else ''
                position = row[2] if len(row) > 2 else ''
                if company and position:
                    existing_keys[(company, position)] = idx

        # Prepare updates and new rows
        updates = []
        new_rows = []

        for key, app in applications.items():
            urls_str = ', '.join(app.get('urls', [])[:2]) if app.get('urls') else ''

            if key in existing_keys:
                # Update existing row
                row_num = existing_keys[key]

                # Update status (Column D)
                updates.append({
                    'range': f'Applications!D{row_num}',
                    'values': [[app['status']]]
                })

                # Update last updated (Column H)
                updates.append({
                    'range': f'Applications!H{row_num}',
                    'values': [[app['last_updated']]]
                })

                # Update URLs if we have them (Column G)
                if urls_str:
                    updates.append({
                        'range': f'Applications!G{row_num}',
                        'values': [[urls_str]]
                    })

            else:
                # New row
                new_rows.append([
                    app['date_applied'],
                    app['company'],
                    app['position'],
                    app['status'],
                    '',  # Days Since Applied (formula added separately)
                    app.get('contact_email', '—'),
                    urls_str,
                    app['last_updated'],
                    ''  # Notes
                ])

        # Apply updates
        if updates:
            body = {'valueInputOption': 'USER_ENTERED', 'data': updates}
            self.sheets_service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body=body
            ).execute()
            print(f"   ✓ Updated {len(updates)} existing rows")

        # Append new rows
        if new_rows:
            body = {'values': new_rows}
            self.sheets_service.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range='Applications!A:I',
                valueInputOption='USER_ENTERED',
                body=body
            ).execute()
            print(f"   ✓ Added {len(new_rows)} new applications")

            # Add formulas for Days Since Applied (Column E)
            start_row = len(existing_rows) + 1
            formula_updates = []
            for i in range(len(new_rows)):
                row_num = start_row + i
                formula_updates.append({
                    'range': f'Applications!E{row_num}',
                    'values': [[f'=TODAY()-A{row_num}']]
                })

            if formula_updates:
                body = {'valueInputOption': 'USER_ENTERED', 'data': formula_updates}
                self.sheets_service.spreadsheets().values().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body=body
                ).execute()

            # Add follow-up flag formulas (Column J): flags Applied rows with no contact after 7 days
            followup_updates = []
            for i in range(len(new_rows)):
                row_num = start_row + i
                followup_updates.append({
                    'range': f'Applications!J{row_num}',
                    'values': [[f'=IF(AND(D{row_num}="Applied",E{row_num}>7),"⚠️ Follow Up","✓")']]
                })
            if followup_updates:
                body = {'valueInputOption': 'USER_ENTERED', 'data': followup_updates}
                self.sheets_service.spreadsheets().values().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body=body
                ).execute()

        print("✅ Google Sheets updated successfully\n")

    # Mapping from detected status to Gmail label
    STATUS_LABELS = {
        'Applied': 'Jobs/Applied',
        'Interview': 'Jobs/Interview',
        'Rejected': 'Jobs/Rejected',
        'Offer': 'Jobs/Offer',
        'Under Review': 'Jobs/Under Review',
    }

    def get_label_id(self, label_name):
        """Get Gmail label ID by name"""
        try:
            results = self.gmail_service.users().labels().list(userId='me').execute()
            labels = results.get('labels', [])
            for label in labels:
                if label['name'] == label_name:
                    return label['id']
        except Exception as e:
            print(f"Error getting label ID: {e}")
        return None

    def get_or_create_label(self, label_name):
        """Get a Gmail label ID, creating the label if it doesn't exist"""
        label_id = self.get_label_id(label_name)
        if label_id:
            return label_id

        try:
            label_body = {
                'name': label_name,
                'labelListVisibility': 'labelShow',
                'messageListVisibility': 'show',
            }
            result = self.gmail_service.users().labels().create(
                userId='me', body=label_body
            ).execute()
            print(f"   Created Gmail label: {label_name}")
            return result['id']
        except Exception as e:
            print(f"⚠️  Could not create label '{label_name}': {e}")
            return None

    def ensure_status_labels_exist(self):
        """Pre-create all status labels so they're ready to use"""
        print("🏷️  Verifying status labels...")
        for status, label_name in self.STATUS_LABELS.items():
            self.get_or_create_label(label_name)
        print("   ✓ All status labels ready")

    def label_emails_by_status(self, message_ids, msg_statuses, dry_run=False):
        """
        Move processed emails to the correct status label (Jobs/Applied, Jobs/Interview, etc.)
        Removes Jobs/Active and Jobs/Archive labels so every tracked email lands in exactly
        one status bucket regardless of where it currently lives.
        """
        if not message_ids:
            return

        # Get label IDs for the source labels we want to clean up
        active_label_id = self.get_label_id('Jobs/Active')
        archive_label_id = self.get_label_id('Jobs/Archive')

        # Pre-resolve all status label IDs
        status_label_ids = {}
        for status, label_name in self.STATUS_LABELS.items():
            label_id = self.get_or_create_label(label_name)
            if label_id:
                status_label_ids[status] = label_id

        # All source + status label IDs eligible for removal
        # 'INBOX' is a system label — removing it archives the email out of inbox
        all_status_label_ids = list(status_label_ids.values())
        source_label_ids = [lid for lid in [active_label_id, archive_label_id, 'INBOX'] if lid]

        labeled_counts = {}
        for msg_id in message_ids:
            status = msg_statuses.get(msg_id, 'Applied')
            target_label_id = status_label_ids.get(status)

            if not target_label_id:
                continue

            try:
                # Get current labels for this message
                msg = self.gmail_service.users().messages().get(
                    userId='me',
                    id=msg_id,
                    format='minimal'
                ).execute()

                current_labels = msg.get('labelIds', [])

                # Remove Jobs/Active, Jobs/Archive, and any old status labels
                # then add the correct status label
                remove_ids = [
                    lid for lid in source_label_ids + all_status_label_ids
                    if lid in current_labels and lid != target_label_id
                ]

                if dry_run:
                    print(f"   [DRY RUN] {msg_id} → {self.STATUS_LABELS[status]}")
                else:
                    self.gmail_service.users().messages().modify(
                        userId='me',
                        id=msg_id,
                        body={
                            'removeLabelIds': remove_ids,
                            'addLabelIds': [target_label_id]
                        }
                    ).execute()

                labeled_counts[status] = labeled_counts.get(status, 0) + 1
            except Exception as e:
                pass

        if labeled_counts:
            total = sum(labeled_counts.values())
            prefix = "[DRY RUN] " if dry_run else ""
            print(f"🏷️  {prefix}Labeled {total} emails from Jobs/Active:")
            for status, count in sorted(labeled_counts.items()):
                print(f"   → Jobs/{status}: {count}")

    def scan_gmail_test(self, max_emails=10):
        """
        Test scan - process first N emails and show what would be extracted
        """
        print(f"\n🧪 TEST SCAN - Processing first {max_emails} emails...")
        print("="*80)

        query = '(label:Jobs OR label:Jobs-Active OR label:Jobs-Archive) after:2025/01/01'

        try:
            results = self.gmail_service.users().messages().list(
                userId='me',
                q=query,
                maxResults=max_emails
            ).execute()

            messages = results.get('messages', [])
            print(f"Found {len(messages)} emails\n")

            for idx, msg_ref in enumerate(messages, 1):
                msg_id = msg_ref['id']
                message = self.gmail_service.users().messages().get(
                    userId='me',
                    id=msg_id,
                    format='full'
                ).execute()

                # Extract headers
                headers = message['payload']['headers']
                subject = ''
                sender = ''
                date = ''

                for header in headers:
                    if header['name'] == 'Subject':
                        subject = header['value']
                    elif header['name'] == 'From':
                        sender = header['value']
                    elif header['name'] == 'Date':
                        date = header['value']

                # Skip self-sent emails
                if 'wmelendez215@gmail.com' in sender:
                    print(f"[{idx}/{max_emails}] SKIPPED (self-sent): {subject[:60]}\n")
                    continue

                # Skip known non-job senders
                sender_lower = sender.lower()
                if any(s in sender_lower for s in NON_JOB_SENDERS):
                    print(f"[{idx}/{max_emails}] SKIPPED (non-job sender): {subject[:60]}\n")
                    continue

                # Extract body
                body = self.get_email_body(message)

                # Try AI extraction first, fall back to regex
                ai_result = self.extract_with_ai(subject, body, sender)
                
                if ai_result and ai_result.get('is_job_application') != False:
                    # AI successfully extracted data
                    company = ai_result.get('company')
                    position = ai_result.get('position')
                    confidence = ai_result.get('confidence', 'medium')
                    source = 'openai'
                else:
                    # Fall back to regex extraction
                    company, confidence, source = self.extract_company_from_email(subject, body, sender)
                    position = self.extract_position_from_email(subject, body)
                
                status = self.determine_status_from_email(subject, body)

                print(f"[{idx}/{max_emails}] {subject[:60]}")
                print(f"   Sender: {sender}")
                print(f"   Company: {company} (confidence: {confidence}, source: {source})")

                # Validate
                if company:
                    is_valid_co, co_reason = self.is_valid_company_name(company)
                    if not is_valid_co:
                        print(f"   ❌ INVALID COMPANY: {co_reason}")
                        company = None

                if position:
                    is_valid_pos, pos_reason = self.is_valid_position_title(position)
                    if not is_valid_pos:
                        print(f"   ❌ INVALID POSITION: {pos_reason}")
                        position = None

                print(f"   Position: {position}")
                print(f"   Status: {status}")

                if not company or not position:
                    print(f"   ⚠️  WOULD SKIP: Missing data")
                else:
                    print(f"   ✅ WOULD ADD")

                print()

            print("="*80)
            print("Test complete. Review the output above.")
            print("If extractions look accurate, run full scan with --full flag")

        except Exception as e:
            print(f"Error: {e}")

    def tag_inbox_job_emails(self):
        """
        Scan inbox for emails from known job/ATS senders and apply Jobs/Active label.
        This ensures new job emails are picked up even without a Gmail filter in place.
        LinkedIn is intentionally excluded from the broad sender list — application
        confirmations are rescued separately via rescue_linkedin_applications().
        """
        print("📥 Scanning inbox for untagged job emails...")

        # Known ATS platforms and employer domains that send job application emails.
        # LinkedIn is excluded here — its confirmation emails are rescued from Jobs/Tracker
        # separately so that job suggestion emails are left alone and stay visible.
        known_senders = [
            'indeed.com', 'workday.com', 'myworkday.com',
            'greenhouse.io', 'lever.co', 'ashbyhq.com', 'icims.com',
            'taleo.net', 'successfactors.com', 'smartrecruiters.com',
            'jobvite.com', 'rippling.com', 'governmentjobs.com',
            'usastaffing.gov', 'phila.gov', 'cityofchicago.org',
            'thecha.org', 'cookcounty.gov', 'delaware.gov', 'illinois.gov',
            'fanniemae.com', 'freddiemac.com', 'howardbrown.org',
            'luriechildrens.org', 'lifetimefitness.com', 'lifetime.life',
            'usastaffingoffice.gov',
            'ns2cloud.com',   # SAP HCM cloud — State of IL, other gov agencies
            'adp.com',        # ADP applicant tracking
        ]
        sender_query = ' OR '.join(f'from:{d}' for d in known_senders)
        subject_query = (
            'subject:"Application Received" OR subject:"thank you for applying" '
            'OR subject:"Employment Application Received" OR subject:"Your application" '
            'OR subject:"Application Confirmation" OR subject:"thanks for your interest in the" '
            'OR subject:"Thank you for your interest in the" OR subject:"application has been submitted"'
        )
        query = f'in:inbox after:2025/01/01 ({sender_query} OR {subject_query})'

        try:
            results = self.gmail_service.users().messages().list(
                userId='me', q=query, maxResults=200
            ).execute()
            messages = results.get('messages', [])

            if not messages:
                print("   No untagged inbox job emails found.")
                return

            active_label_id = self.get_or_create_label('Jobs/Active')
            if not active_label_id:
                return

            labeled = 0
            for msg_ref in messages:
                msg = self.gmail_service.users().messages().get(
                    userId='me', id=msg_ref['id'], format='minimal'
                ).execute()
                current_labels = msg.get('labelIds', [])
                if active_label_id not in current_labels:
                    # Add Jobs/Active and remove INBOX so the email leaves the inbox immediately
                    remove_ids = ['INBOX'] if 'INBOX' in current_labels else []
                    self.gmail_service.users().messages().modify(
                        userId='me',
                        id=msg_ref['id'],
                        body={'addLabelIds': [active_label_id], 'removeLabelIds': remove_ids}
                    ).execute()
                    labeled += 1

            if labeled:
                print(f"   ✓ Tagged {labeled} inbox emails as Jobs/Active (archived from inbox)")
            else:
                print("   All inbox job emails already labeled.")

        except Exception as e:
            print(f"   ⚠️  Inbox scan error: {e}")

    def clean_tracker_label(self):
        """
        Automatically sort Jobs/Tracker every run:
          - Job alerts (LinkedIn, Indeed suggestions) → Jobs/Alerts
          - Noise (self-sent, replies, DMs, account emails) → remove label (archive)
          - Legitimate applications → stay in Jobs/Tracker for scanning
        This runs before every scan so Gmail filter rules can't re-pollute the label.
        """
        print("🧹 Cleaning Jobs/Tracker...")

        TRACKER_LABEL = self.get_label_id('Jobs/Tracker')
        ALERTS_LABEL  = self.get_or_create_label('Jobs/Alerts')
        if not TRACKER_LABEL or not ALERTS_LABEL:
            return

        ALERT_SENDERS = [
            'jobalerts-noreply@linkedin', 'jobalerts@linkedin',
            'linkedin job alerts', 'jobs-noreply@linkedin',
            'alert@indeed.com', 'alert@indeedemail.com',
        ]
        ALERT_SUBJECT_PATTERNS = [
            'is hiring for', 'jobs you may like', 'new jobs for you',
            'job alert', 'recommended jobs', 'jobs matching',
            '": ',   # LinkedIn alert format: "job title": Company
        ]
        NOISE_SENDERS = ['wmelendez215@gmail.com']
        NOISE_SUBJECTS = [
            'just messaged you', 'candidate home account', 'join talent community',
            'stay connected', '1 new message awaits',
        ]
        NOISE_PREFIXES = ['re: ', 'fwd: ', 'fw: ', 'automatic reply', 'auto-reply']

        all_msgs = []
        page_token = None
        while True:
            kwargs = dict(userId='me', q='label:Jobs-Tracker', maxResults=500)
            if page_token:
                kwargs['pageToken'] = page_token
            r = self.gmail_service.users().messages().list(**kwargs).execute()
            all_msgs.extend(r.get('messages', []))
            page_token = r.get('nextPageToken')
            if not page_token:
                break

        to_alerts = 0
        removed = 0
        kept = 0

        for msg_ref in all_msgs:
            try:
                msg = self.gmail_service.users().messages().get(
                    userId='me', id=msg_ref['id'],
                    format='metadata', metadataHeaders=['Subject', 'From']
                ).execute()
                headers  = {h['name']: h['value'] for h in msg['payload']['headers']}
                subject  = headers.get('Subject', '').lower()
                sender   = headers.get('From', '').lower()

                is_alert = (
                    any(p in sender  for p in ALERT_SENDERS) or
                    any(p in subject for p in ALERT_SUBJECT_PATTERNS)
                )
                is_noise = (
                    any(p in sender  for p in NOISE_SENDERS) or
                    any(p in subject for p in NOISE_SUBJECTS) or
                    any(subject.startswith(p) for p in NOISE_PREFIXES)
                )

                if is_alert:
                    self.gmail_service.users().messages().modify(
                        userId='me', id=msg_ref['id'],
                        body={'addLabelIds': [ALERTS_LABEL], 'removeLabelIds': [TRACKER_LABEL]}
                    ).execute()
                    to_alerts += 1
                elif is_noise:
                    self.gmail_service.users().messages().modify(
                        userId='me', id=msg_ref['id'],
                        body={'removeLabelIds': [TRACKER_LABEL]}
                    ).execute()
                    removed += 1
                else:
                    kept += 1
            except Exception:
                continue

        print(f"   → {to_alerts} alerts moved to Jobs/Alerts")
        if removed:
            print(f"   → {removed} noise emails removed")
        print(f"   ✓ {kept} legitimate applications remain in Jobs/Tracker")

    def rescue_linkedin_applications(self):
        """
        Rescue LinkedIn application confirmation emails from Jobs/Tracker.

        LinkedIn sends two types of emails:
          - Job suggestions ("New jobs for you", "Jobs you may like") → stay in Jobs/Tracker
          - Application confirmations ("Your application to [Role] at [Company]") → move to Jobs/Active

        Gmail filters typically dump all LinkedIn mail into Jobs/Tracker. This method
        selectively moves only the confirmations so the tracker can log them, while
        leaving suggestion emails visible in Jobs/Tracker for browsing.
        """
        print("🔗 Rescuing LinkedIn application confirmations from Jobs/Tracker...")

        # Only match the exact LinkedIn confirmation subject prefix
        query = 'label:Jobs-Tracker from:linkedin.com subject:"Your application to" after:2025/01/01'

        try:
            results = self.gmail_service.users().messages().list(
                userId='me', q=query, maxResults=200
            ).execute()
            messages = results.get('messages', [])

            if not messages:
                print("   No LinkedIn confirmations found in Jobs/Tracker.")
                return

            active_label_id = self.get_or_create_label('Jobs/Active')
            tracker_label_id = self.get_label_id('Jobs/Tracker')
            if not active_label_id:
                return

            moved = 0
            for msg_ref in messages:
                msg = self.gmail_service.users().messages().get(
                    userId='me', id=msg_ref['id'], format='minimal'
                ).execute()
                current_labels = msg.get('labelIds', [])
                if active_label_id not in current_labels:
                    modify_body = {'addLabelIds': [active_label_id]}
                    if tracker_label_id and tracker_label_id in current_labels:
                        modify_body['removeLabelIds'] = [tracker_label_id]
                    self.gmail_service.users().messages().modify(
                        userId='me',
                        id=msg_ref['id'],
                        body=modify_body
                    ).execute()
                    moved += 1

            if moved:
                print(f"   ✓ Moved {moved} LinkedIn confirmations → Jobs/Active")
            else:
                print("   All LinkedIn confirmations already in Jobs/Active.")

        except Exception as e:
            print(f"   ⚠️  LinkedIn rescue error: {e}")


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description='Accurate Job Tracker V2')
    parser.add_argument('--test', type=int, help='Test mode - process N emails and show what would be extracted', default=None)
    parser.add_argument('--days', type=int, help='Number of days back to scan (default: 14)', default=14)
    parser.add_argument('--full', action='store_true', help='Full historical scan (365 days)')
    parser.add_argument('--sweep', action='store_true', help='Sweep ALL Gmail folders for missed job emails, then do full scan')
    args = parser.parse_args(argv)

    tracker = AccurateJobTracker()
    tracker.authenticate_gmail()

    if args.test:
        tracker.scan_gmail_test(max_emails=args.test)
    else:
        days_back = 365 if (args.full or args.sweep) else args.days
        if args.sweep:
            # Sweep every folder in Gmail for untagged job emails first
            tracker.sweep_all_gmail(days_back=days_back)
        # Clean Jobs/Tracker first (move alerts → Jobs/Alerts, remove noise)
        tracker.clean_tracker_label()
        # Tag inbox job emails, rescue LinkedIn confirmations from Jobs/Tracker, then scan
        tracker.tag_inbox_job_emails()
        tracker.rescue_linkedin_applications()
        try:
            tracker.authenticate_sheets()
        except Exception as e:
            print(f"❌ Google Sheets authentication failed: {e}")
            print("   Run `python3 ~/sheets_reauth.py` manually, then rerun the tracker.")
            return
        tracker.scan_gmail_full(days_back=days_back)
        print("\n✅ Job Tracker Complete")

if __name__ == '__main__':
    main()
