import http.client
import logging
import socket
import time
import xmlrpc.client

from odoo import _, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL

_logger = logging.getLogger(__name__)


class NkSkip(Exception):
    """Raised to skip a record without treating it as a failure."""


class _TimeoutTransport(xmlrpc.client.SafeTransport):
    """HTTPS transport with a socket timeout and no connection reuse.

    A fresh connection per call means a dropped keep-alive socket can never
    leave the client in the 'Request-sent' state."""

    def __init__(self, timeout=90):
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn

    def close(self):
        self._connection = (None, None)
        super().close()

    def single_request(self, host, handler, request_body, verbose=False):
        try:
            return super().single_request(host, handler, request_body, verbose)
        finally:
            self.close()


_RETRY_ERRORS = (
    http.client.ResponseNotReady,
    http.client.CannotSendRequest,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
    http.client.BadStatusLine,
    ConnectionError,
    socket.timeout,
    TimeoutError,
)
_CHUNK = 100


class _Source:
    """Thin read-only XML-RPC client for the source Odoo database."""

    def __init__(self, url, db, login, key):
        url = url.rstrip('/')
        self.url = url
        common = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common', allow_none=True,
                                           transport=_TimeoutTransport())
        self.uid = common.authenticate(db, login, key, {})
        if not self.uid:
            raise UserError(_('Source login failed. Check URL, database, login and API key.'))
        self.version = common.version().get('server_version')
        self.db, self.key = db, key
        self._connect()
        self._fields = {}

    def _connect(self):
        self.proxy = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/object', allow_none=True,
                                               transport=_TimeoutTransport())

    def call(self, model, method, *args, **kw):
        """Every call here is read-only, so retrying on transport errors is safe."""
        last = None
        for attempt in range(4):
            try:
                return self.proxy.execute_kw(self.db, self.uid, self.key, model, method, list(args), kw)
            except _RETRY_ERRORS as e:
                last = e
                _logger.warning('Source call %s.%s failed (%s), retry %s/3', model, method, e, attempt + 1)
                self._connect()
                time.sleep(2 ** attempt)
        raise last

    def fields(self, model):
        if model not in self._fields:
            self._fields[model] = self.call(model, 'fields_get', attributes=['type'])
        return self._fields[model]

    def search(self, model, domain, **kw):
        return self.call(model, 'search', domain, **kw)

    def read(self, model, ids, field_names):
        if not ids:
            return []
        known = self.fields(model)
        field_names = [f for f in field_names if f in known]
        ids = list(ids)
        out = []
        for i in range(0, len(ids), _CHUNK):
            out.extend(self.call(model, 'read', ids[i:i + _CHUNK], fields=field_names))
        return out

    def first_field(self, model, candidates):
        known = self.fields(model)
        return next((f for f in candidates if f in known), None)


def _m2o(value):
    return value[0] if value else False


TAX_FIELDS = ('tax_ids', 'tax_id', 'taxes_id')

KINDS = {
    'sale': {
        'model': 'sale.order',
        'line_model': 'sale.order.line',
        'line_field': 'order_line',
        'order_fields': ['name', 'partner_id', 'date_order', 'state', 'locked', 'client_order_ref',
                         'note', 'order_line', 'invoice_ids', 'picking_ids', 'create_date',
                         'validity_date', 'commitment_date'],
        'copy': ['client_order_ref', 'note', 'validity_date', 'commitment_date'],
        'line_fields': ['sequence', 'display_type', 'product_id', 'name', 'product_uom_qty',
                        'price_unit', 'discount'],
        'qty': 'product_uom_qty',
        'move_link': 'sale_line_id',
        'inv_link': 'sale_line_ids',
    },
    'purchase': {
        'model': 'purchase.order',
        'line_model': 'purchase.order.line',
        'line_field': 'order_line',
        'order_fields': ['name', 'partner_id', 'date_order', 'state', 'locked', 'partner_ref',
                         'notes', 'order_line', 'invoice_ids', 'picking_ids', 'create_date',
                         'date_approve'],
        'copy': ['partner_ref', 'notes', 'date_approve'],
        'line_fields': ['sequence', 'display_type', 'product_id', 'name', 'product_qty',
                        'price_unit', 'date_planned'],
        'qty': 'product_qty',
        'move_link': 'purchase_line_id',
        'inv_link': 'purchase_line_id',
    },
}


class NkMigrationLog(models.Model):
    _name = 'nk.migration.log'
    _description = 'Migration Log'
    _order = 'id desc'

    run_id = fields.Many2one('nk.migration.run', ondelete='cascade', index=True)
    res_model = fields.Char(string='Record Type')
    source_ref = fields.Char(string='Source Reference')
    status = fields.Selection([('ok', 'Success'), ('skipped', 'Skipped'), ('failed', 'Failed')])
    message = fields.Text()


class NkMigrationMap(models.Model):
    _name = 'nk.migration.map'
    _description = 'Source to Destination ID Mapping'

    source_key = fields.Char(index=True)
    res_model = fields.Char(index=True)
    src_id = fields.Integer(index=True)
    dest_id = fields.Integer()

    _uniq = models.Constraint('UNIQUE(source_key, res_model, src_id)', 'This record is already mapped.')


class NkMigrationRun(models.Model):
    _name = 'nk.migration.run'
    _description = 'Data Migration Run'
    _order = 'id desc'

    name = fields.Char(required=True, default='Migration')
    source_url = fields.Char(required=True)
    source_db = fields.Char(string='Source Database', required=True)
    source_login = fields.Char(required=True)
    source_key = fields.Char(string='API Key', required=True)
    date_from = fields.Date(required=True)
    date_to = fields.Date(required=True)
    do_sales = fields.Boolean(string='Sales Orders', default=True)
    do_purchase = fields.Boolean(string='Purchase Orders', default=True)
    do_pickings = fields.Boolean(string='Deliveries / Receipts', default=True)
    do_invoices = fields.Boolean(string='Invoices / Bills', default=True)
    product_match = fields.Selection(
        [('code', 'Internal Reference (name if code is empty)'), ('name', 'Product Name only')],
        string='Match Products By', default='code', required=True,
        help='How source products are found in this database. Name matching needs exactly the same '
             'name and no duplicate names.')
    duplicate_policy = fields.Selection(
        [('fail', 'Fail (safe)'), ('first', 'Use the first match (oldest active product)')],
        string='If Product Name is Duplicate', default='fail', required=True,
        help='When two or more destination products have the same name: fail the order, or link to '
             'the oldest active one. Every such case is written in the Migration Report for review.')
    batch_size = fields.Integer(
        string='Records per click', default=50,
        help='Max new orders / transfers / invoices created per click. '
             'Click Start again to continue; already migrated records are skipped.')
    state = fields.Selection(
        [('draft', 'Draft'), ('previewed', 'Previewed'), ('migrated', 'Migrated')],
        default='draft')
    log_ids = fields.One2many('nk.migration.log', 'run_id', string='Report')

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def action_test_connection(self):
        self.ensure_one()
        src = self._source()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Connection OK'),
                'message': _('Connected to source (Odoo %s).', src.version),
                'type': 'success',
            },
        }

    def action_preview(self):
        self.ensure_one()
        self.log_ids.unlink()
        self._run(dry=True)
        return True

    def action_start(self):
        self.ensure_one()
        self._run(dry=False)
        return True

    # ------------------------------------------------------------------
    # Helpers: connection, mapping, logging
    # ------------------------------------------------------------------
    def _source(self):
        self.ensure_one()
        try:
            return _Source(self.source_url, self.source_db, self.source_login, self.source_key)
        except (OSError, xmlrpc.client.Error) as e:
            raise UserError(_('Cannot connect to source: %s', e)) from e

    def _skey(self):
        return f"{self.source_url.rstrip('/')}|{self.source_db}"

    def _map_get(self, model, src_id):
        rec = self.env['nk.migration.map'].search([
            ('source_key', '=', self._skey()), ('res_model', '=', model), ('src_id', '=', src_id)], limit=1)
        if rec and self.env[model].browse(rec.dest_id).exists():
            return rec.dest_id
        return False

    def _map_set(self, model, src_id, dest_id):
        Map = self.env['nk.migration.map']
        rec = Map.search([
            ('source_key', '=', self._skey()), ('res_model', '=', model), ('src_id', '=', src_id)], limit=1)
        if rec:
            rec.dest_id = dest_id
        else:
            Map.create({'source_key': self._skey(), 'res_model': model,
                        'src_id': src_id, 'dest_id': dest_id})

    def _keep_created(self, rec, value):
        """Keep the source 'Created on' timestamp on the migrated record."""
        if value:
            self.env.cr.execute(SQL('UPDATE %s SET create_date = %s WHERE id = %s',
                                    SQL.identifier(rec._table), value, rec.id))
            rec.invalidate_recordset(['create_date'])

    def _log(self, model, ref, status, message=''):
        self.env['nk.migration.log'].create({
            'run_id': self.id, 'res_model': model, 'source_ref': ref,
            'status': status, 'message': message or ''})

    def _process(self, model, ref, func):
        """Run func in a savepoint; log the outcome; commit so progress survives timeouts."""
        try:
            with self.env.cr.savepoint():
                message = func()
            self._log(model, ref, 'ok', message)
        except NkSkip as e:
            self._log(model, ref, 'skipped', str(e))
        except Exception as e:  # noqa: BLE001 - we want every failure in the report
            _logger.exception('Migration failed for %s %s', model, ref)
            self._log(model, ref, 'failed', str(e))
        self.env.cr.commit()

    # ------------------------------------------------------------------
    # Master data matching (partners, products, taxes)
    # ------------------------------------------------------------------
    def _partner(self, src, pid, create=True):
        mapped = self._map_get('res.partner', pid)
        if mapped:
            return mapped
        p = src.read('res.partner', [pid], ['name', 'email', 'vat', 'phone', 'street', 'street2',
                                            'city', 'zip', 'country_id', 'is_company'])[0]
        Partner = self.env['res.partner'].with_context(active_test=False)
        found = Partner
        if p.get('vat'):
            found = Partner.search([('vat', '=', p['vat'])], limit=1)
        if not found and p.get('email'):
            found = Partner.search([('email', '=ilike', p['email']), ('name', '=ilike', p['name'])], limit=1)
        if not found:
            found = Partner.search([('name', '=ilike', p['name'])], limit=1)
        if not found:
            if not create:
                return False
            country = self.env['res.country']
            if p.get('country_id'):
                country = country.search([('name', '=', p['country_id'][1])], limit=1)
            found = Partner.create({
                'name': p['name'], 'email': p.get('email') or False, 'vat': p.get('vat') or False,
                'phone': p.get('phone') or False, 'street': p.get('street') or False,
                'street2': p.get('street2') or False, 'city': p.get('city') or False,
                'zip': p.get('zip') or False, 'country_id': country.id,
                'is_company': p.get('is_company', False)})
        self._map_set('res.partner', pid, found.id)
        return found.id

    def _product(self, src, pid):
        mapped = self._map_get('product.product', pid)
        if mapped:
            return mapped
        p = src.read('product.product', [pid], ['default_code', 'name'])[0]
        Product = self.env['product.product'].with_context(active_test=False)
        if self.product_match == 'code' and p.get('default_code'):
            found = Product.search([('default_code', '=', p['default_code'])], limit=1)
        else:
            found = Product.search([('name', '=', p['name'])], order='active desc, id')
            if len(found) > 1:
                active = found.filtered('active')
                if len(active) == 1:
                    found = active
                elif self.duplicate_policy == 'first':
                    found = found[:1]
                    self._log('product.product', p['name'], 'skipped',
                              _('Duplicate product name: linked to the oldest one (ID %s). Please review.', found.id))
                else:
                    raise UserError(_('More than one product named "%s" in destination. '
                                      'Archive the duplicate or set "If Product Name is Duplicate" to use the first match.',
                                      p['name']))
        if not found:
            raise UserError(_('Product not found in destination: [%s] %s', p.get('default_code') or '-', p['name']))
        self._map_set('product.product', pid, found.id)
        return found.id

    def _taxes(self, src, tax_ids):
        Tax = self.env['account.tax']
        company = self.env.company
        result = []
        for t in src.read('account.tax', tax_ids, ['name', 'amount', 'amount_type', 'type_tax_use']):
            base = [('type_tax_use', '=', t['type_tax_use']), ('company_id', '=', company.id)]
            found = Tax.search(base + [('name', '=', t['name'])], limit=1) or Tax.search(
                base + [('amount', '=', t['amount']), ('amount_type', '=', t['amount_type'])], limit=1)
            if not found:
                raise UserError(_('Tax not found in destination: %s (%s%%)', t['name'], t['amount']))
            result.append(found.id)
        return result

    # ------------------------------------------------------------------
    # Main flow
    # ------------------------------------------------------------------
    def _run(self, dry):
        self = self.with_context(nk_dry=dry)
        src = self._source()
        if self.do_sales:
            self._migrate_orders(src, 'sale')
        if self.do_purchase:
            self._migrate_orders(src, 'purchase')
        self.state = 'previewed' if dry else 'migrated'

    def _migrate_orders(self, src, kind):
        cfg = KINDS[kind]
        dry = self.env.context.get('nk_dry')
        model = cfg['model']
        domain = [('date_order', '>=', f'{self.date_from} 00:00:00'),
                  ('date_order', '<=', f'{self.date_to} 23:59:59')]
        orders = src.read(model, src.search(model, domain, order='id'), cfg['order_fields'])

        # Pass 1: orders
        created = 0
        for o in orders:
            if self._map_get(model, o['id']):
                if dry:
                    self._log(model, o['name'], 'skipped', _('Already migrated'))
                continue
            if dry:
                self._process(model, o['name'], lambda o=o: self._check_order(src, kind, o))
            elif created < self.batch_size:
                self._process(model, o['name'], lambda o=o: self._create_order(src, kind, o))
                created += 1
        if dry:
            return

        # Pass 2: deliveries/receipts and invoices/bills of every migrated order.
        # Reads are batched across orders (not one RPC per order) and skipped
        # entirely once the batch quota is used.
        migrated = [o for o in orders if self._map_get(model, o['id'])]
        n_pick = n_inv = 0

        if self.do_pickings and migrated:
            owner = {}
            for o in migrated:
                for pid in o.get('picking_ids', []):
                    owner[pid] = o
            todo = [pid for pid in sorted(owner) if not self._map_get('stock.picking', pid)]
            for sp in src.read('stock.picking', todo[:self.batch_size],
                               ['name', 'state', 'origin', 'partner_id', 'scheduled_date', 'date_done',
                                'picking_type_code', 'move_ids', 'create_date']):
                self._process('stock.picking', sp['name'], lambda sp=sp: self._create_picking(src, kind, sp))
                n_pick += 1

        if self.do_invoices and migrated:
            inv_ids = sorted({i for o in migrated for i in o.get('invoice_ids', [])})
            todo = [i for i in inv_ids if not self._map_get('account.move', i)]
            for inv in src.read('account.move', todo[:self.batch_size],
                                ['name', 'move_type', 'state', 'partner_id', 'invoice_date', 'date',
                                 'invoice_date_due', 'ref', 'payment_reference', 'currency_id',
                                 'invoice_origin', 'invoice_line_ids', 'narration', 'amount_total',
                                 'payment_state', 'create_date']):
                self._process('account.move', inv['name'], lambda inv=inv: self._create_invoice(src, kind, inv))
                n_inv += 1

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------
    def _check_order(self, src, kind, o):
        cfg = KINDS[kind]
        notes = []
        if not self._partner(src, _m2o(o['partner_id']), create=False):
            notes.append(_('Partner will be created.'))
        tax_f = src.first_field(cfg['line_model'], TAX_FIELDS)
        lines = src.read(cfg['line_model'], o['order_line'], ['product_id', 'display_type', tax_f])
        for l in lines:
            if l.get('display_type'):
                continue
            if l.get('product_id'):
                self._product(src, l['product_id'][0])
            if tax_f and l.get(tax_f):
                self._taxes(src, l[tax_f])
        return _('Ready: %(lines)s lines, %(pick)s transfers, %(inv)s invoices/bills. %(notes)s',
                 lines=len(lines), pick=len(o.get('picking_ids', [])),
                 inv=len(o.get('invoice_ids', [])), notes=' '.join(notes))

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def _create_order(self, src, kind, o):
        cfg = KINDS[kind]
        Order = self.env[cfg['model']].with_context(tracking_disable=True, mail_create_nolog=True)
        Line = self.env[cfg['line_model']]
        src_tax = src.first_field(cfg['line_model'], TAX_FIELDS)
        dest_tax = next((f for f in TAX_FIELDS if f in Line._fields), None)

        src_lines = src.read(cfg['line_model'], o['order_line'], cfg['line_fields'] + ([src_tax] if src_tax else []))
        src_lines.sort(key=lambda l: (l.get('sequence', 0), l['id']))

        commands = []
        for l in src_lines:
            vals = {'sequence': l.get('sequence', 10), 'name': l['name'],
                    'display_type': l.get('display_type') or False}
            if not vals['display_type']:
                vals.update({
                    'product_id': self._product(src, l['product_id'][0]) if l.get('product_id') else False,
                    cfg['qty']: l[cfg['qty']],
                    'price_unit': l['price_unit'],
                })
                if 'discount' in l and 'discount' in Line._fields:
                    vals['discount'] = l['discount']
                if l.get('date_planned') and 'date_planned' in Line._fields:
                    vals['date_planned'] = l['date_planned']
                if dest_tax:
                    vals[dest_tax] = [(6, 0, self._taxes(src, l[src_tax]) if src_tax and l.get(src_tax) else [])]
            commands.append((0, 0, vals))

        vals = {
            'name': o['name'],
            'partner_id': self._partner(src, _m2o(o['partner_id'])),
            'date_order': o['date_order'],
            'order_line': commands,
        }
        for f in cfg['copy']:
            if f in Order._fields and o.get(f):
                vals[f] = o[f]
        order = Order.create(vals)

        # Keep the source stage and date exactly. Writing the state directly avoids
        # confirmation side effects (no automatic deliveries are generated).
        if o['state'] in dict(Order._fields['state'].selection):
            order.write({'state': o['state']})
        if o.get('locked') and 'locked' in Order._fields:
            order.write({'locked': True})

        self._keep_created(order, o.get('create_date'))
        self._map_set(cfg['model'], o['id'], order.id)
        for src_line, dest_line in zip(src_lines, order.order_line.sorted('id')):
            self._map_set(cfg['line_model'], src_line['id'], dest_line.id)
        return _('Created %s (%s) with %s lines', order.name, order.state, len(src_lines))

    # ------------------------------------------------------------------
    # Deliveries / Receipts
    # ------------------------------------------------------------------
    def _create_picking(self, src, kind, sp):
        cfg = KINDS[kind]
        company = self.env.company
        ptype = self.env['stock.picking.type'].search([
            ('code', '=', sp['picking_type_code']), ('company_id', '=', company.id)], limit=1)
        if not ptype:
            raise UserError(_('No operation type of kind "%s" in destination.', sp['picking_type_code']))
        loc_src, loc_dest = ptype.default_location_src_id, ptype.default_location_dest_id

        link = cfg['move_link']
        line_model = cfg['line_model']
        src_moves = src.read('stock.move', sp['move_ids'],
                             ['product_id', 'product_uom_qty', 'quantity', 'sequence', link])
        move_cmds, qty_by_key = [], {}
        for m in src_moves:
            product = self.env['product.product'].browse(self._product(src, m['product_id'][0]))
            line_id = self._map_get(line_model, m[link][0]) if m.get(link) else False
            vals = {
                'product_id': product.id,
                'product_uom_qty': m['product_uom_qty'],
                'product_uom': product.uom_id.id,
                'location_id': loc_src.id,
                'location_dest_id': loc_dest.id,
            }
            if line_id and link in self.env['stock.move']._fields:
                vals[link] = line_id
            move_cmds.append((0, 0, vals))
            key = (product.id, line_id)
            qty_by_key[key] = qty_by_key.get(key, 0) + m.get('quantity', 0)

        picking = self.env['stock.picking'].with_context(tracking_disable=True).create({
            'name': sp['name'],
            'picking_type_id': ptype.id,
            'partner_id': self._partner(src, _m2o(sp['partner_id'])) if sp.get('partner_id') else False,
            'origin': sp.get('origin') or False,
            'scheduled_date': sp['scheduled_date'],
            'location_id': loc_src.id,
            'location_dest_id': loc_dest.id,
            'move_ids': move_cmds,
        })

        state = sp['state']
        if state == 'cancel':
            picking.action_cancel()
        elif state in ('confirmed', 'waiting', 'assigned', 'done'):
            picking.action_confirm()
            picking.action_assign()
            if state == 'done':
                for move in picking.move_ids:
                    line_id = move[link].id if link in move._fields and move[link] else False
                    move.quantity = qty_by_key.get((move.product_id.id, line_id), move.product_uom_qty)
                    move.picked = True
                picking.with_context(skip_immediate=True, skip_backorder=True, skip_sms=True).button_validate()
                if picking.state != 'done':
                    raise UserError(_('Could not validate transfer %s.', sp['name']))
                if sp.get('date_done'):
                    picking.write({'date_done': sp['date_done']})
                    picking.move_ids.write({'date': sp['date_done']})
        self._keep_created(picking, sp.get('create_date'))
        self._map_set('stock.picking', sp['id'], picking.id)
        return _('Created %s (%s)', picking.name, picking.state)

    # ------------------------------------------------------------------
    # Invoices / Vendor bills (with line-to-order-line linking)
    # ------------------------------------------------------------------
    def _create_invoice(self, src, kind, inv):
        cfg = KINDS[kind]
        Move = self.env['account.move'].with_context(
            tracking_disable=True, default_move_type=inv['move_type'])
        src_lines = src.read('account.move.line', inv['invoice_line_ids'],
                             ['sequence', 'display_type', 'product_id', 'name', 'quantity', 'price_unit',
                              'discount', 'tax_ids', 'sale_line_ids', 'purchase_line_id'])
        src_lines.sort(key=lambda l: (l.get('sequence', 0), l['id']))

        notes, commands = [], []
        for l in src_lines:
            dtype = l.get('display_type')
            if dtype in ('line_section', 'line_note'):
                commands.append((0, 0, {'display_type': dtype, 'name': l['name']}))
                continue
            vals = {
                'product_id': self._product(src, l['product_id'][0]) if l.get('product_id') else False,
                'name': l['name'],
                'quantity': l['quantity'],
                'price_unit': l['price_unit'],
                'discount': l.get('discount', 0.0),
                'tax_ids': [(6, 0, self._taxes(src, l['tax_ids']) if l.get('tax_ids') else [])],
            }
            if kind == 'sale':
                dest_ids = [self._map_get('sale.order.line', i) for i in l.get('sale_line_ids', [])]
                dest_ids = [i for i in dest_ids if i]
                if len(dest_ids) != len(l.get('sale_line_ids', [])):
                    notes.append(_('Line "%s": linked order line not migrated.', l['name']))
                vals['sale_line_ids'] = [(6, 0, dest_ids)]
            elif l.get('purchase_line_id'):
                dest = self._map_get('purchase.order.line', l['purchase_line_id'][0])
                if dest:
                    vals['purchase_line_id'] = dest
                else:
                    notes.append(_('Line "%s": linked order line not migrated.', l['name']))
            commands.append((0, 0, vals))

        vals = {
            'move_type': inv['move_type'],
            'partner_id': self._partner(src, _m2o(inv['partner_id'])),
            'invoice_date': inv.get('invoice_date') or False,
            'invoice_date_due': inv.get('invoice_date_due') or False,
            'ref': inv.get('ref') or False,
            'payment_reference': inv.get('payment_reference') or False,
            'invoice_origin': inv.get('invoice_origin') or False,
            'narration': inv.get('narration') or False,
            'invoice_line_ids': commands,
        }
        if inv.get('currency_id'):
            cur = self.env['res.currency'].with_context(active_test=False).search(
                [('name', '=', inv['currency_id'][1])], limit=1)
            if cur:
                vals['currency_id'] = cur.id
        if inv.get('date'):
            vals['date'] = inv['date']
        move = Move.create(vals)

        if inv['state'] == 'posted':
            try:
                with self.env.cr.savepoint():
                    move.name = inv['name']  # keep the source invoice number
                    move.action_post()
            except Exception as e:  # noqa: BLE001
                notes.append(_('Source number %s could not be kept (%s); new number assigned.', inv['name'], e))
                move.action_post()
        elif inv['state'] == 'cancel':
            move.button_cancel()

        if inv['state'] == 'posted' and abs(move.amount_total - inv['amount_total']) > 0.01:
            notes.append(_('Total differs: source %s vs destination %s.', inv['amount_total'], move.amount_total))
        if inv.get('payment_state') in ('paid', 'in_payment', 'partial'):
            notes.append(_('Source invoice was %s: payment is NOT migrated yet (create it manually).',
                           inv['payment_state']))

        self._keep_created(move, inv.get('create_date'))
        self._map_set('account.move', inv['id'], move.id)
        return _('Created %s (%s). %s', move.name, move.state, ' '.join(notes))
