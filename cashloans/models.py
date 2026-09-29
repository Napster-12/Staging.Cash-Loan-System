import calendar
from datetime import date, datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

PERMS = [
    ('customers', 'Add & edit customers'),
    ('apply', 'Capture loan applications'),
    ('approve', 'Approve / reject / write off loans'),
    ('payments', 'Record payments'),
    ('reports', 'View & download reports'),
    ('users', 'Manage staff users'),
]

# Every step a loan can move through, in the order it happens.
EVENTS = [
    ('captured', 'Application captured'),
    ('approved', 'Approved'),
    ('rejected', 'Rejected'),
    ('written_off', 'Written off'),
    ('payment', 'Payment received'),
    ('settled', 'Fully repaid'),
]

EVENT_LABELS = dict(EVENTS)

# Natural order of a loan's life, used to sequence events that share a timestamp.
LIFECYCLE = ['captured', 'approved', 'payment', 'written_off', 'settled', 'rejected']

# The role a person was acting in when they made each kind of entry.
EVENT_ROLES = {
    'captured': 'Captured by',
    'approved': 'Approved by',
    'rejected': 'Rejected by',
    'payment': 'Payment taken by',
    'written_off': 'Written off by',
    'settled': 'Closed by',
}


def add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(80), nullable=False, default='')
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default='staff')          # owner | staff
    permissions = db.Column(db.Text, default='')
    active = db.Column(db.Boolean, default=True)
    must_change = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def is_active(self):
        return bool(self.active)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    def can(self, perm):
        return self.active and (self.role == 'owner' or perm in (self.permissions or '').split(','))

    @property
    def is_owner(self):
        return self.role == 'owner'

    @property
    def display(self):
        return self.name or self.email


class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(60), nullable=False)
    last_name = db.Column(db.String(60), nullable=False)
    id_number = db.Column(db.String(30), unique=True, nullable=False)
    phone = db.Column(db.String(30), nullable=False)
    email = db.Column(db.String(120))
    address = db.Column(db.String(255))
    employer = db.Column(db.String(120))
    monthly_income = db.Column(db.Float, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    loans = db.relationship('Loan', backref='customer', order_by='Loan.id.desc()')

    @property
    def full_name(self):
        return f'{self.first_name} {self.last_name}'

    @property
    def open_loan(self):
        return next((l for l in self.loans if l.status in ('pending', 'active')), None)

    @property
    def has_default(self):
        return any(l.status == 'defaulted' for l in self.loans)

    @property
    def outstanding(self):
        return round(sum(l.balance for l in self.loans if l.status == 'active'), 2)


class Loan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(20), unique=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False)
    principal = db.Column(db.Float, nullable=False)
    monthly_rate = db.Column(db.Float, nullable=False)        # flat % of principal per month
    term_months = db.Column(db.Integer, nullable=False)
    purpose = db.Column(db.String(200))
    status = db.Column(db.String(20), default='pending')      # pending|active|paid|rejected|defaulted
    note = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    decided_at = db.Column(db.DateTime)
    decided_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    start_date = db.Column(db.Date)
    created_by = db.relationship('User', foreign_keys=[created_by_id])
    decided_by = db.relationship('User', foreign_keys=[decided_by_id])
    installments = db.relationship('Installment', backref='loan', order_by='Installment.number',
                                   cascade='all, delete-orphan')
    payments = db.relationship('Payment', backref='loan', order_by='Payment.id.desc()',
                               cascade='all, delete-orphan')

    @property
    def total_interest(self):
        return round(self.principal * self.monthly_rate / 100 * self.term_months, 2)

    @property
    def total_repayment(self):
        return round(self.principal + self.total_interest, 2)

    @property
    def instalment_amount(self):
        return round(self.total_repayment / self.term_months, 2)

    @property
    def total_paid(self):
        return round(sum(i.paid for i in self.installments), 2)

    @property
    def balance(self):
        if not self.installments:
            return self.total_repayment if self.status in ('pending', 'active') else 0
        return round(sum(i.outstanding for i in self.installments), 2)

    @property
    def progress(self):
        return int(self.total_paid / self.total_repayment * 100) if self.installments and self.total_repayment else 0

    @property
    def overdue_items(self):
        if self.status != 'active':
            return []
        return [i for i in self.installments if i.outstanding > 0 and i.due_date < date.today()]

    @property
    def overdue_amount(self):
        return round(sum(i.outstanding for i in self.overdue_items), 2)

    @property
    def days_overdue(self):
        items = self.overdue_items
        return (date.today() - items[0].due_date).days if items else 0

    @property
    def next_due(self):
        return next((i for i in self.installments if i.outstanding > 0), None)

    @property
    def display_status(self):
        return 'overdue' if self.overdue_items else self.status

    def approve(self, user, note=''):
        self.status, self.decided_at, self.decided_by, self.note = 'active', datetime.utcnow(), user, note
        self.start_date = date.today()
        total, n = self.total_repayment, self.term_months
        each = self.instalment_amount
        self.installments = []
        for k in range(1, n + 1):
            amt = each if k < n else round(total - each * (n - 1), 2)
            self.installments.append(Installment(number=k, due_date=add_months(self.start_date, k), amount=amt))

    def take_payment(self, amount):
        left = amount
        for i in self.installments:
            if left <= 0:
                break
            take = min(i.outstanding, left)
            i.paid = round(i.paid + take, 2)
            left = round(left - take, 2)
        if self.balance <= 0.005:
            self.status = 'paid'


class Installment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('loan.id'), nullable=False)
    number = db.Column(db.Integer, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    paid = db.Column(db.Float, default=0)

    @property
    def outstanding(self):
        return round(max(self.amount - (self.paid or 0), 0), 2)

    @property
    def state(self):
        if self.outstanding <= 0:
            return 'paid'
        if self.due_date < date.today():
            return 'overdue'
        return 'partial' if self.paid else 'upcoming'


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('loan.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(20), default='cash')
    reference = db.Column(db.String(60))
    notes = db.Column(db.String(255))
    paid_at = db.Column(db.DateTime, default=datetime.utcnow)
    recorded_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    recorded_by = db.relationship('User')


class AuditEvent(db.Model):
    """One immutable entry in a loan's history: what happened, and who did it."""
    __tablename__ = 'audit_event'

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('loan.id'), nullable=False, index=True)
    event = db.Column(db.String(20), nullable=False, index=True)   # see EVENTS
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    actor_name = db.Column(db.String(80))      # kept so history survives staff deletion
    amount = db.Column(db.Float)               # payment value / balance moved
    balance_after = db.Column(db.Float)        # loan balance once this step completed
    from_status = db.Column(db.String(20))
    to_status = db.Column(db.String(20))
    method = db.Column(db.String(20))
    note = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    loan = db.relationship('Loan', backref=db.backref('audit_events', order_by='AuditEvent.id',
                                                      cascade='all, delete-orphan'))
    actor = db.relationship('User')

    @property
    def label(self):
        return EVENT_LABELS.get(self.event, self.event)

    @property
    def role(self):
        """Who was acting in when this step was taken, e.g. 'Approved by'."""
        return EVENT_ROLES.get(self.event, 'Handled by')

    @property
    def who(self):
        return self.actor_name or (self.actor.display if self.actor else 'System')
