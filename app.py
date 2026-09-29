import os
import csv
import secrets
from io import StringIO
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for, flash,
                   Response, session, abort)
from flask_login import (LoginManager, login_user, logout_user, login_required,
                         current_user)
from sqlalchemy import func

from models import (db, User, Customer, Loan, Payment, PERMS, EVENTS, EVENT_LABELS,
                    LIFECYCLE, AuditEvent, add_months)

BASE = os.path.abspath(os.path.dirname(__file__))
os.makedirs(os.path.join(BASE, 'instance'), exist_ok=True)

COMPANY = 'Codnell Cash Loans'
DEFAULT_RATE = 20.0      # % of principal per month (flat) - change to suit your product
MAX_PRINCIPAL = 50000
MAX_TERM = 12


def _secret():
    path = os.path.join(BASE, 'instance', 'secret.key')
    if not os.path.exists(path):
        with open(path, 'w') as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get('SECRET_KEY') or _secret(),
    SQLALCHEMY_DATABASE_URI=os.environ.get('DATABASE_URL')
    or 'sqlite:///' + os.path.join(BASE, 'instance', 'loans.db'),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)
db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please sign in to continue.'


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


# ---------- helpers ----------
def csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_hex(16)
    return session['_csrf']


app.jinja_env.globals.update(csrf_token=csrf_token, COMPANY=COMPANY, PERMS=PERMS,
                             today=date.today, DEFAULT_RATE=DEFAULT_RATE,
                             MAX_PRINCIPAL=MAX_PRINCIPAL, MAX_TERM=MAX_TERM)
app.jinja_env.filters['money'] = lambda v: 'R {:,.2f}'.format(v or 0)
app.jinja_env.filters['d'] = lambda v: v.strftime('%d %b %Y') if v else '-'


@app.before_request
def guard():
    if request.method == 'POST':
        if not session.get('_csrf') or session['_csrf'] != request.form.get('_csrf'):
            abort(400, 'Your session expired. Go back, refresh the page and try again.')
    if (current_user.is_authenticated and current_user.must_change
            and request.endpoint not in ('profile', 'logout', 'static')):
        return redirect(url_for('profile'))


def need(perm=None):
    def deco(f):
        @wraps(f)
        @login_required
        def inner(*a, **k):
            if perm and not current_user.can(perm):
                flash("You don't have access to that page.", 'error')
                return redirect(url_for('dashboard'))
            return f(*a, **k)
        return inner
    return deco


def need_owner(f):
    """Owner-only: full super rights over staff accounts and their passwords."""
    @wraps(f)
    @login_required
    def inner(*a, **k):
        if not current_user.is_owner:
            flash('Only the owner can do that.', 'error')
            return redirect(url_for('users'))
        return f(*a, **k)
    return inner


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def err(e):
    return render_template('error.html', e=e), e.code


def num(name, default=0.0):
    try:
        return float(request.form.get(name, default))
    except (TypeError, ValueError):
        return None


def log_event(loan, event, actor, amount=None, from_status=None, to_status=None,
              method=None, note=None, balance_after=None, when=None):
    """Append an immutable audit entry for a step in a loan's life."""
    e = AuditEvent(loan=loan, event=event, actor=actor,
                   actor_name=(actor.display if actor else None),
                   amount=amount, from_status=from_status, to_status=to_status,
                   method=method, note=note or None, created_at=when or datetime.utcnow())
    # balance_after defaults to the loan's balance once the step has been applied
    if balance_after is None:
        balance_after = loan.balance
    e.balance_after = round(balance_after, 2)
    db.session.add(e)
    return e


def backfill_audit():
    """Create audit entries for loans that pre-date the audit trail, from existing data.

    Payments are replayed in date order and each row records the balance as it stood
    at that moment, so the reconstructed history reads correctly.
    """
    order = {k: i for i, k in enumerate(LIFECYCLE)}
    made = 0
    for l in Loan.query.all():
        if AuditEvent.query.filter_by(loan_id=l.id).first():
            continue
        total = l.total_repayment
        pays = sorted(l.payments, key=lambda x: (x.paid_at or datetime.min, x.id))
        running = 0.0
        steps = [('captured', l.created_by, l.principal, None, 'pending', l.purpose,
                  l.created_at, total, None)]
        if l.decided_at or l.status in ('rejected', 'defaulted'):
            rejected = l.status == 'rejected'
            # some records pre-date decision tracking, so fall back to the capture date
            when = l.decided_at or l.created_at
            steps.append(((('rejected') if rejected else 'approved'), l.decided_by, l.principal,
                          'pending', ('rejected' if rejected else 'active'), l.note,
                          when, 0 if rejected else total, None))
        for p in pays:
            running += p.amount or 0
            steps.append(('payment', p.recorded_by, p.amount, None, None,
                          p.notes or p.reference, p.paid_at, round(total - running, 2),
                          p.method))
        # close the loan out on the date of its last real movement
        last_when = max([s[6] for s in steps if s[6]] or [l.created_at])
        if l.status == 'paid':
            steps.append(('settled', l.decided_by, 0, 'active', 'paid',
                          'Loan fully repaid', last_when, 0, None))
        elif l.status == 'defaulted':
            steps.append(('written_off', l.decided_by, round(total - running, 2),
                          'active', 'defaulted', l.note, last_when,
                          round(total - running, 2), None))
        steps.sort(key=lambda s: (s[6] or datetime.min, order.get(s[0], 99)))
        for event, actor, amount, frm, to, note, when, bal, method in steps:
            log_event(l, event, actor, amount=amount, from_status=frm, to_status=to,
                      method=method, note=note, when=when, balance_after=max(bal, 0))
            made += 1
    db.session.commit()
    return made


def seed():
    db.create_all()
    email = os.environ.get('OWNER_EMAIL', 'codnellsmall@gmail.com').lower()
    if not User.query.first():
        u = User(email=email, name='Owner', role='owner', must_change=True)
        u.set_password(os.environ.get('OWNER_PASSWORD', 'ChangeMe123!'))
        db.session.add(u)
        db.session.commit()
        print(f'\n  First run: sign in as {email} with the temporary password '
              f'(you will be asked to change it).\n')


with app.app_context():
    seed()


# ---------- auth ----------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        u = User.query.filter_by(email=request.form.get('email', '').strip().lower()).first()
        if u and u.active and u.check_password(request.form.get('password', '')):
            login_user(u)
            return redirect(url_for('dashboard'))
        flash('Wrong email or password.', 'error')
    return render_template('login.html')


@app.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        new, cur = request.form.get('new', ''), request.form.get('current', '')
        if not current_user.check_password(cur):
            flash('Current password is incorrect.', 'error')
        elif len(new) < 8:
            flash('New password must be at least 8 characters.', 'error')
        elif new != request.form.get('confirm'):
            flash('New password and confirmation do not match.', 'error')
        elif new == cur:
            flash('New password must be different from the current one.', 'error')
        else:
            current_user.name = request.form.get('name', '').strip() or current_user.name
            current_user.set_password(new)
            current_user.must_change = False
            db.session.commit()
            # re-verify the hash actually took, so a silent save failure is visible
            db.session.refresh(current_user)
            if not current_user.check_password(new):
                flash('Could not save the new password. Please try again.', 'error')
                return render_template('profile.html')
            flash('Password changed.', 'success')
            return redirect(url_for('dashboard'))
    return render_template('profile.html')


# ---------- dashboard ----------
@app.route('/')
@login_required
def dashboard():
    t = date.today()
    active = Loan.query.filter_by(status='active').all()
    overdue = sorted([l for l in active if l.overdue_items], key=lambda l: -l.days_overdue)
    pending = Loan.query.filter_by(status='pending').order_by(Loan.created_at).all()
    month_start = datetime.combine(t.replace(day=1), datetime.min.time())
    collected = db.session.query(func.coalesce(func.sum(Payment.amount), 0)) \
        .filter(Payment.paid_at >= month_start).scalar()
    due_soon = sorted([(l, l.next_due) for l in active
                       if l.next_due and t <= l.next_due.due_date <= t + timedelta(days=7)],
                      key=lambda x: x[1].due_date)
    closed = Loan.query.filter(Loan.status.in_(['paid', 'defaulted'])).all()
    default_rate = (sum(1 for l in closed if l.status == 'defaulted') / len(closed) * 100) if closed else 0
    all_loans = Loan.query.filter(Loan.start_date.isnot(None)).all()
    pays = Payment.query.all()
    months = []
    for k in range(5, -1, -1):
        m = add_months(t.replace(day=1), -k)
        out = sum(l.principal for l in all_loans if (l.start_date.year, l.start_date.month) == (m.year, m.month))
        inn = sum(p.amount for p in pays if (p.paid_at.year, p.paid_at.month) == (m.year, m.month))
        months.append(dict(label=m.strftime('%b'), out=out, inn=inn))
    top = max([max(x['out'], x['inn']) for x in months] + [1])
    for x in months:
        x['oh'], x['ih'] = round(x['out'] / top * 100), round(x['inn'] / top * 100)
    return render_template('dashboard.html', active=active, overdue=overdue, pending=pending,
                           collected=collected, due_soon=due_soon, default_rate=default_rate,
                           book=sum(l.balance for l in active),
                           overdue_total=sum(l.overdue_amount for l in overdue),
                           months=months, customers=Customer.query.count())


# ---------- customers ----------
def _customer_from_form(c):
    f = request.form
    c.first_name, c.last_name = f['first_name'].strip(), f['last_name'].strip()
    c.id_number, c.phone = f['id_number'].strip(), f['phone'].strip()
    c.email, c.address = f.get('email', '').strip().lower() or None, f.get('address', '').strip()
    c.employer, c.monthly_income = f.get('employer', '').strip(), num('monthly_income') or 0


def _customer_form(c, title):
    if request.method == 'POST':
        clash = Customer.query.filter(Customer.id_number == request.form['id_number'].strip(),
                                      Customer.id != (c.id or 0)).first()
        if clash:
            flash(f'A customer with that ID number already exists ({clash.full_name}).', 'error')
        else:
            _customer_from_form(c)
            db.session.add(c)
            db.session.commit()
            flash('Customer saved.', 'success')
            return redirect(url_for('customer_view', cid=c.id))
    return render_template('customer_form.html', c=c, title=title)


@app.route('/customers')
@need()
def customers():
    q = request.args.get('q', '').strip()
    query = Customer.query
    if q:
        like = f'%{q}%'
        query = query.filter(Customer.first_name.ilike(like) | Customer.last_name.ilike(like) |
                             Customer.phone.ilike(like) | Customer.id_number.ilike(like))
    return render_template('customers.html', rows=query.order_by(Customer.created_at.desc()).all(), q=q)


@app.route('/customers/new', methods=['GET', 'POST'])
@need('customers')
def customer_new():
    return _customer_form(Customer(), 'New customer')


@app.route('/customers/<int:cid>')
@need()
def customer_view(cid):
    return render_template('customer_view.html', c=db.get_or_404(Customer, cid))


@app.route('/customers/<int:cid>/edit', methods=['GET', 'POST'])
@need('customers')
def customer_edit(cid):
    return _customer_form(db.get_or_404(Customer, cid), 'Edit customer')


# ---------- loans ----------
STATUSES = ['pending', 'active', 'overdue', 'paid', 'rejected', 'defaulted']


@app.route('/loans')
@need()
def loans():
    st, q = request.args.get('status', ''), request.args.get('q', '').strip().lower()
    rows = Loan.query.order_by(Loan.created_at.desc()).all()
    counts = {s: sum(1 for l in rows if l.display_status == s) for s in STATUSES}
    counts['active'] = sum(1 for l in rows if l.status == 'active')
    if st == 'active':
        rows = [l for l in rows if l.status == 'active']
    elif st:
        rows = [l for l in rows if l.display_status == st]
    if q:
        rows = [l for l in rows if q in l.customer.full_name.lower() or q in (l.reference or '').lower()]
    return render_template('loans.html', rows=rows, st=st, q=q, counts=counts, statuses=STATUSES)


@app.route('/loans/new', methods=['GET', 'POST'])
@need('apply')
def loan_new():
    cid = request.values.get('customer_id', type=int)
    customers_ = Customer.query.order_by(Customer.first_name).all()
    if request.method == 'POST':
        c = db.session.get(Customer, cid or 0)
        amount, rate, term = num('principal'), num('monthly_rate'), request.form.get('term_months', type=int)
        err_ = None
        if not c:
            err_ = 'Choose a customer.'
        elif c.open_loan:
            err_ = f'{c.full_name} already has a {c.open_loan.status} loan ({c.open_loan.reference}).'
        elif c.has_default:
            err_ = f'{c.full_name} has a defaulted loan and cannot borrow again.'
        elif not amount or amount <= 0 or amount > MAX_PRINCIPAL:
            err_ = f'Amount must be between R 1 and R {MAX_PRINCIPAL:,}.'
        elif rate is None or rate < 0 or rate > 100:
            err_ = 'Interest rate must be between 0 and 100.'
        elif not term or not 1 <= term <= MAX_TERM:
            err_ = f'Term must be 1 to {MAX_TERM} months.'
        if err_:
            flash(err_, 'error')
        else:
            l = Loan(customer=c, principal=round(amount, 2), monthly_rate=rate, term_months=term,
                     purpose=request.form.get('purpose', '').strip(), created_by=current_user)
            db.session.add(l)
            db.session.flush()
            l.reference = f'LN-{l.id:05d}'
            log_event(l, 'captured', current_user, amount=l.principal,
                      to_status='pending', note=l.purpose, balance_after=l.total_repayment)
            db.session.commit()
            flash('Application captured. It is waiting for approval.', 'success')
            return redirect(url_for('loan_view', lid=l.id))
    return render_template('loan_form.html', customers=customers_, cid=cid)


@app.route('/loans/<int:lid>')
@need()
def loan_view(lid):
    return render_template('loan_view.html', l=db.get_or_404(Loan, lid))


@app.route('/loans/<int:lid>/decide', methods=['POST'])
@need('approve')
def loan_decide(lid):
    l = db.get_or_404(Loan, lid)
    action, note = request.form.get('action'), request.form.get('note', '').strip()
    if l.status != 'pending':
        flash('This application has already been decided.', 'error')
    elif l.created_by_id == current_user.id and current_user.role != 'owner':
        flash('Someone else must approve an application you captured.', 'error')
    elif action == 'approve':
        l.approve(current_user, note)
        log_event(l, 'approved', current_user, amount=l.principal,
                  from_status='pending', to_status='active', note=note)
        db.session.commit()
        flash('Loan approved. The repayment schedule has been created.', 'success')
    elif action == 'reject':
        if not note:
            flash('Give a reason for rejecting.', 'error')
            return redirect(url_for('loan_view', lid=lid))
        l.status, l.note, l.decided_at, l.decided_by = 'rejected', note, datetime.utcnow(), current_user
        log_event(l, 'rejected', current_user, amount=l.principal,
                  from_status='pending', to_status='rejected', note=note, balance_after=0)
        db.session.commit()
        flash('Application rejected.', 'success')
    return redirect(url_for('loan_view', lid=lid))


@app.route('/loans/<int:lid>/writeoff', methods=['POST'])
@need('approve')
def loan_writeoff(lid):
    l = db.get_or_404(Loan, lid)
    if l.status == 'active':
        note = request.form.get('note', '').strip() or 'Written off'
        l.status, l.note = 'defaulted', note
        log_event(l, 'written_off', current_user, amount=l.balance,
                  from_status='active', to_status='defaulted', note=note)
        db.session.commit()
        flash('Loan written off. Outstanding balance: R {:,.2f}'.format(l.balance), 'success')
    return redirect(url_for('loan_view', lid=lid))


# ---------- payments ----------
@app.route('/loans/<int:lid>/pay', methods=['GET', 'POST'])
@need('payments')
def payment_new(lid):
    l = db.get_or_404(Loan, lid)
    if l.status != 'active':
        flash('Payments can only be recorded on active loans.', 'error')
        return redirect(url_for('loan_view', lid=lid))
    if request.method == 'POST':
        amt = num('amount')
        if not amt or amt <= 0:
            flash('Enter a payment amount.', 'error')
        elif amt > l.balance + 0.005:
            flash(f'That is more than the balance of R {l.balance:,.2f}.', 'error')
        else:
            p = Payment(loan=l, amount=round(amt, 2), method=request.form.get('method', 'cash'),
                        reference=request.form.get('reference', '').strip(),
                        notes=request.form.get('notes', '').strip(), recorded_by=current_user)
            db.session.add(p)
            l.take_payment(round(amt, 2))
            log_event(l, 'payment', current_user, amount=p.amount, method=p.method,
                      from_status='active', to_status=l.status, note=p.notes or p.reference)
            if l.status == 'paid':
                log_event(l, 'settled', current_user, amount=0, from_status='active',
                          to_status='paid', note='Loan fully repaid', balance_after=0)
            db.session.commit()
            flash('Payment recorded.' + (' The loan is now fully paid.' if l.status == 'paid' else ''), 'success')
            return redirect(url_for('loan_view', lid=lid))
    suggest = l.overdue_amount or (l.next_due.outstanding if l.next_due else l.balance)
    return render_template('payment_form.html', l=l, suggest=suggest)


@app.route('/payments')
@need('payments')
def payments():
    return render_template('payments.html', rows=Payment.query.order_by(Payment.paid_at.desc()).limit(300).all())


@app.route('/collections')
@need()
def collections():
    rows = sorted([l for l in Loan.query.filter_by(status='active').all() if l.overdue_items],
                  key=lambda l: -l.days_overdue)
    return render_template('collections.html', rows=rows)


# ---------- reports ----------
@app.route('/reports')
@need('reports')
def reports():
    loans_ = Loan.query.all()
    disbursed = [l for l in loans_ if l.start_date]
    return render_template('reports.html',
                           disbursed=sum(l.principal for l in disbursed),
                           interest=sum(l.total_interest for l in disbursed),
                           collected=db.session.query(func.coalesce(func.sum(Payment.amount), 0)).scalar(),
                           book=sum(l.balance for l in loans_ if l.status == 'active'),
                           written_off=sum(l.balance for l in loans_ if l.status == 'defaulted'),
                           counts={s: sum(1 for l in loans_ if l.display_status == s) for s in STATUSES})


# ---------- audit trail (owner only) ----------
def _audit_query():
    """Build the audit query from the request's filters."""
    q = AuditEvent.query
    ev = request.args.get('event', '')
    who = request.args.get('who', '')
    ref = request.args.get('ref', '').strip()
    st = request.args.get('status', '')
    frm = request.args.get('from', '')
    to = request.args.get('to', '')
    if ev:
        q = q.filter(AuditEvent.event == ev)
    if who:
        q = q.filter(AuditEvent.actor_id == who)
    if st:
        q = q.filter(AuditEvent.loan.has(Loan.status == st))
    if ref:
        like = f'%{ref}%'
        q = q.filter(AuditEvent.loan.has(
            Loan.reference.ilike(like) |
            Loan.customer_id.in_(
                db.session.query(Customer.id).filter(
                    Customer.first_name.ilike(like) | Customer.last_name.ilike(like) |
                    Customer.phone.ilike(like) | Customer.id_number.ilike(like)))))
    if frm:
        try:
            q = q.filter(AuditEvent.created_at >= datetime.combine(date.fromisoformat(frm), datetime.min.time()))
        except ValueError:
            pass
    if to:
        try:
            d = date.fromisoformat(to)
            q = q.filter(AuditEvent.created_at <= datetime.combine(d, datetime.max.time()))
        except ValueError:
            pass
    return q.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())


@app.route('/audit')
@need_owner
def audit():
    q = _audit_query()
    rows = q.limit(500).all()
    total = q.count()
    shown = len(rows)
    # totals across the *filtered* set, so the summary matches what is listed
    collected = sum(e.amount for e in rows if e.event == 'payment')
    actors = User.query.order_by(User.name).all()
    counts = {k: q.filter(AuditEvent.event == k).count() for k, _ in EVENTS}
    return render_template('audit.html', rows=rows, events=EVENTS, actors=actors,
                           counts=counts, total=total, shown=shown, collected=collected,
                           statuses=STATUSES, f=_audit_filters(),
                           has_events=AuditEvent.query.first() is not None)


def _audit_filters():
    a = request.args
    return dict(event=a.get('event', ''), who=a.get('who', ''), ref=a.get('ref', ''),
                status=a.get('status', ''), frm=a.get('from', ''), to=a.get('to', ''))


@app.route('/audit/backfill', methods=['POST'])
@need_owner
def audit_backfill():
    made = backfill_audit()
    flash(f'Backfilled {made} audit entries from existing loan and payment records.', 'success')
    return redirect(url_for('audit'))


@app.route('/audit.csv')
@need_owner
def audit_csv():
    out = StringIO()
    w = csv.writer(out)
    w.writerow(['When', 'Step', 'Loan', 'Customer', 'Handled by', 'Amount', 'Balance after',
                'From status', 'To status', 'Method', 'Note'])
    for e in _audit_query().all():
        w.writerow([e.created_at.strftime('%Y-%m-%d %H:%M'), e.label, e.loan.reference,
                    e.loan.customer.full_name, e.who, e.amount, e.balance_after,
                    e.from_status or '', e.to_status or '', e.method or '', e.note or ''])
    return Response(out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename=audit-{date.today()}.csv'})


@app.route('/loans/<int:lid>/audit')
@need()
def loan_audit(lid):
    l = db.get_or_404(Loan, lid)
    return render_template('loan_audit.html', l=l, rows=l.audit_events)


@app.route('/reports/<kind>.csv')
@need('reports')
def report_csv(kind):
    out = StringIO()
    w = csv.writer(out)
    if kind == 'loans':
        w.writerow(['Reference', 'Customer', 'Principal', 'Monthly rate %', 'Term', 'Total repayable',
                    'Paid', 'Balance', 'Status', 'Start date'])
        for l in Loan.query.all():
            w.writerow([l.reference, l.customer.full_name, l.principal, l.monthly_rate, l.term_months,
                        l.total_repayment, l.total_paid, l.balance, l.display_status, l.start_date or ''])
    elif kind == 'payments':
        w.writerow(['Date', 'Loan', 'Customer', 'Amount', 'Method', 'Reference', 'Recorded by'])
        for p in Payment.query.order_by(Payment.paid_at).all():
            w.writerow([p.paid_at.strftime('%Y-%m-%d %H:%M'), p.loan.reference, p.loan.customer.full_name,
                        p.amount, p.method, p.reference or '', p.recorded_by.display if p.recorded_by else ''])
    elif kind == 'overdue':
        w.writerow(['Loan', 'Customer', 'Phone', 'Overdue amount', 'Days overdue', 'Balance'])
        for l in Loan.query.filter_by(status='active').all():
            if l.overdue_items:
                w.writerow([l.reference, l.customer.full_name, l.customer.phone, l.overdue_amount,
                            l.days_overdue, l.balance])
    elif kind == 'customers':
        w.writerow(['Name', 'ID number', 'Phone', 'Email', 'Employer', 'Income', 'Outstanding'])
        for c in Customer.query.all():
            w.writerow([c.full_name, c.id_number, c.phone, c.email or '', c.employer or '',
                        c.monthly_income, c.outstanding])
    else:
        abort(404)
    return Response(out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename={kind}-{date.today()}.csv'})


# ---------- staff ----------
@app.route('/users', methods=['GET', 'POST'])
@need('users')
def users():
    if request.method == 'POST':
        if not current_user.is_owner:
            flash('Only the owner can add staff members.', 'error')
            return redirect(url_for('users'))
        email = request.form['email'].strip().lower()
        pw = request.form['password']
        if User.query.filter_by(email=email).first():
            flash('A user with that email already exists.', 'error')
        elif len(pw) < 8:
            flash('Temporary password must be at least 8 characters.', 'error')
        else:
            u = User(email=email, name=request.form.get('name', '').strip(), role='staff', must_change=True,
                     permissions=','.join(p for p in request.form.getlist('perms') if p in dict(PERMS)))
            u.set_password(pw)
            db.session.add(u)
            db.session.commit()
            flash('Staff member added. They must change the password at first sign-in.', 'success')
        return redirect(url_for('users'))
    return render_template('users.html', rows=User.query.order_by(User.created_at).all())


@app.route('/users/<int:uid>', methods=['GET', 'POST'])
@need('users')
def user_edit(uid):
    u = db.get_or_404(User, uid)
    if u.role == 'owner' and current_user.id != u.id:
        flash("The owner account can't be changed by others.", 'error')
        return redirect(url_for('users'))
    if request.method == 'POST':
        u.name = request.form.get('name', '').strip()
        if u.role != 'owner':
            u.permissions = ','.join(p for p in request.form.getlist('perms') if p in dict(PERMS))
            u.active = 'active' in request.form
        # Passwords are never changed here: owners use the Set password panel,
        # everyone else uses /profile (which requires the current password).
        if request.form.get('password'):
            flash("Use the 'Set password' panel, or your account page, to change a password.", 'error')
            return redirect(url_for('user_edit', uid=uid))
        db.session.commit()
        flash('Saved.', 'success')
        return redirect(url_for('users'))
    return render_template('user_edit.html', u=u)


@app.route('/users/<int:uid>/password', methods=['POST'])
@need_owner
def user_password(uid):
    """Owner-only: set any staff member's password directly."""
    u = db.get_or_404(User, uid)
    pw, confirm = request.form.get('password', ''), request.form.get('password_confirm', '')
    if len(pw) < 8:
        flash('Password must be at least 8 characters.', 'error')
    elif pw != confirm:
        flash('The two passwords do not match.', 'error')
    else:
        u.set_password(pw)
        # never force a change on the owner themselves - that would lock them out
        u.must_change = 'force_change' in request.form and u.id != current_user.id
        db.session.commit()
        flash(f"Password updated for {u.display}."
              + (' They must change it at next sign-in.' if u.must_change else ''), 'success')
    return redirect(url_for('user_edit', uid=uid))


if __name__ == '__main__':
    app.run(debug=True)
