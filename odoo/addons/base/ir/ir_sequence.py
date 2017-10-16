# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.
from datetime import datetime, timedelta
import logging
import pytz

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


def _create_sequence(cr, seq_name, number_increment, number_next):
    """ Create a PostreSQL sequence. """
    if number_increment == 0:
        raise UserError(_('Step must not be zero.'))
    sql = "CREATE SEQUENCE %s INCREMENT BY %%s START WITH %%s" % seq_name
    cr.execute(sql, (number_increment, number_next))


def _drop_sequences(cr, seq_names):
    """ Drop the PostreSQL sequences if they exist. """
    names = ','.join(seq_names)
    # RESTRICT is the default; it prevents dropping the sequence if an
    # object depends on it.
    cr.execute("DROP SEQUENCE IF EXISTS %s RESTRICT " % names)


def _alter_sequence(cr, seq_name, number_increment=None, number_next=None):
    """ Alter a PostreSQL sequence. """
    if number_increment == 0:
        raise UserError(_("Step must not be zero."))
    cr.execute("SELECT relname FROM pg_class WHERE relkind=%s AND relname=%s", ('S', seq_name))
    if not cr.fetchone():
        # sequence is not created yet, we're inside create() so ignore it, will be set later
        return
    statement = "ALTER SEQUENCE %s" % (seq_name, )
    if number_increment is not None:
        statement += " INCREMENT BY %d" % (number_increment, )
    if number_next is not None:
        statement += " RESTART WITH %d" % (number_next, )
    cr.execute(statement)


def _select_nextval(cr, seq_name):
    cr.execute("SELECT nextval('%s')" % seq_name)
    return cr.fetchone()


def _update_nogap(self, number_increment):
    number_next = self.number_next
    self._cr.execute("SELECT number_next FROM %s WHERE id=%s FOR UPDATE NOWAIT" % (self._table, self.id))
    self._cr.execute("UPDATE %s SET number_next=number_next+%s WHERE id=%s " % (self._table, number_increment, self.id))
    self.invalidate_cache(['number_next'], [self.id])
    return number_next


class IrSequence(models.Model):
    """ Sequence model.

    The sequence model allows to define and use so-called sequence objects.
    Such objects are used to generate unique identifiers in a transaction-safe
    way.

    """
    _name = 'ir.sequence'
    _order = 'name'

    def _get_number_next_actual(self):
        '''Return number from ir_sequence row when no_gap implementation,
        and number from postgres sequence when standard implementation.'''
        for seq in self:
            if seq.implementation != 'standard':
                seq.number_next_actual = seq.number_next
            else:
                # get number from postgres sequence. Cannot use currval, because that might give an error when
                # not having used nextval before.
                query = "SELECT last_value, increment_by, is_called FROM ir_sequence_%03d" % seq.id
                self._cr.execute(query)
                (last_value, increment_by, is_called) = self._cr.fetchone()
                if is_called:
                    seq.number_next_actual = last_value + increment_by
                else:
                    seq.number_next_actual = last_value

    def _set_number_next_actual(self):
        for seq in self:
            seq.write({'number_next': seq.number_next_actual or 0})

    name = fields.Char(required=True)
    code = fields.Char(string='Sequence Code')
    implementation = fields.Selection([('standard', 'Standard'), ('no_gap', 'No gap')],
                                      string='Implementation', required=True, default='standard',
                                      help="Two sequence object implementations are offered: Standard "
                                           "and 'No gap'. The later is slower than the former but forbids any"
                                           "gap in the sequence (while they are possible in the former).")
    active = fields.Boolean(default=True)
    prefix = fields.Char(help="Prefix value of the record for the sequence")
    suffix = fields.Char(help="Suffix value of the record for the sequence")
    number_next = fields.Integer(string='Next Number', required=True, default=1, help="Next number of this sequence")
    number_next_actual = fields.Integer(compute='_get_number_next_actual', inverse='_set_number_next_actual',
                                        string='Next Number',
                                        help="Next number that will be used. This number can be incremented "
                                        "frequently so the displayed value might already be obsolete")
    number_increment = fields.Integer(string='Step', required=True, default=1,
                                      help="The next number of the sequence will be incremented by this number")
    padding = fields.Integer(string='Sequence Size', required=True, default=0,
                             help="Odoo will automatically adds some '0' on the left of the "
                                  "'Next Number' to get the required padding size.")
    company_id = fields.Many2one('res.company', string='Company',
                                 default=lambda s: s.env['res.company']._company_default_get('ir.sequence'))
    use_date_range = fields.Boolean(string='Use subsequences per date_range')
    date_range_ids = fields.One2many('ir.sequence.date_range', 'sequence_id', string='Subsequences')

    @api.model
    def create(self, values):
        """ Create a sequence, in implementation == standard a fast gaps-allowed PostgreSQL sequence is used.
        """
        seq = super(IrSequence, self).create(values)
        if values.get('implementation', 'standard') == 'standard':
            _create_sequence(self._cr, "ir_sequence_%03d" % seq.id, values.get('number_increment', 1), values.get('number_next', 1))
        return seq

    @api.multi
    def unlink(self):
        _drop_sequences(self._cr, ["ir_sequence_%03d" % x.id for x in self])
        return super(IrSequence, self).unlink()

    @api.multi
    def write(self, values):
        new_implementation = values.get('implementation')
        for seq in self:
            # 4 cases: we test the previous impl. against the new one.
            i = values.get('number_increment', seq.number_increment)
            n = values.get('number_next', seq.number_next)
            if seq.implementation == 'standard':
                if new_implementation in ('standard', None):
                    # Implementation has NOT changed.
                    # Only change sequence if really requested.
                    if values.get('number_next'):
                        _alter_sequence(self._cr, "ir_sequence_%03d" % seq.id, number_next=n)
                    if seq.number_increment != i:
                        _alter_sequence(self._cr, "ir_sequence_%03d" % seq.id, number_increment=i)
                        seq.date_range_ids._alter_sequence(number_increment=i)
                else:
                    _drop_sequences(self._cr, ["ir_sequence_%03d" % seq.id])
                    for sub_seq in seq.date_range_ids:
                        _drop_sequences(self._cr, ["ir_sequence_%03d_%03d" % (seq.id, sub_seq.id)])
            else:
                if new_implementation in ('no_gap', None):
                    pass
                else:
                    _create_sequence(self._cr, "ir_sequence_%03d" % seq.id, i, n)
                    for sub_seq in seq.date_range_ids:
                        _create_sequence(self._cr, "ir_sequence_%03d_%03d" % (seq.id, sub_seq.id), i, n)
        return super(IrSequence, self).write(values)

    def _next_do(self):
        if self.implementation == 'standard':
            number_next = _select_nextval(self._cr, 'ir_sequence_%03d' % self.id)
        else:
            number_next = _update_nogap(self, self.number_increment)
        return self.get_next_char(number_next)

    def get_next_char(self, number_next):
        def _interpolate(s, d):
            return (s % d) if s else ''

        def _interpolation_dict():
            now = range_date = effective_date = datetime.now(pytz.timezone(self._context.get('tz') or 'UTC'))
            if self._context.get('ir_sequence_date'):
                effective_date = datetime.strptime(self._context.get('ir_sequence_date'), '%Y-%m-%d')
            if self._context.get('ir_sequence_date_range'):
                range_date = datetime.strptime(self._context.get('ir_sequence_date_range'), '%Y-%m-%d')

            sequences = {
                'year': '%Y', 'month': '%m', 'day': '%d', 'y': '%y', 'doy': '%j', 'woy': '%W',
                'weekday': '%w', 'h24': '%H', 'h12': '%I', 'min': '%M', 'sec': '%S'
            }
            res = {}
            for key, format in sequences.iteritems():
                res[key] = effective_date.strftime(format)
                res['range_' + key] = range_date.strftime(format)
                res['current_' + key] = now.strftime(format)

            return res

        d = _interpolation_dict()
        try:
            interpolated_prefix = _interpolate(self.prefix, d)
            interpolated_suffix = _interpolate(self.suffix, d)
        except ValueError:
            raise UserError(_('Invalid prefix or suffix for sequence \'%s\'') % (self.get('name')))
        return interpolated_prefix + '%%0%sd' % self.padding % number_next + interpolated_suffix

    def _create_date_range_seq(self, date):
        year = fields.Date.from_string(date).strftime('%Y')
        date_from = '{}-01-01'.format(year)
        date_to = '{}-12-31'.format(year)
        date_range = self.env['ir.sequence.date_range'].search([('sequence_id', '=', self.id), ('date_from', '>=', date), ('date_from', '<=', date_to)], order='date_from desc', limit=1)
        if date_range:
            date_to = datetime.strptime(date_range.date_from, '%Y-%m-%d') + timedelta(days=-1)
            date_to = date_to.strftime('%Y-%m-%d')
        date_range = self.env['ir.sequence.date_range'].search([('sequence_id', '=', self.id), ('date_to', '>=', date_from), ('date_to', '<=', date)], order='date_to desc', limit=1)
        if date_range:
            date_from = datetime.strptime(date_range.date_to, '%Y-%m-%d') + timedelta(days=1)
            date_from = date_from.strftime('%Y-%m-%d')
        seq_date_range = self.env['ir.sequence.date_range'].sudo().create({
            'date_from': date_from,
            'date_to': date_to,
            'sequence_id': self.id,
        })
        return seq_date_range

    def _next(self):
        """ Returns the next number in the preferred sequence in all the ones given in self."""
        if not self.use_date_range:
            return self._next_do()
        # date mode
        dt = fields.Date.today()
        if self._context.get('ir_sequence_date'):
            dt = self._context.get('ir_sequence_date')
        seq_date = self.env['ir.sequence.date_range'].search([('sequence_id', '=', self.id), ('date_from', '<=', dt), ('date_to', '>=', dt)], limit=1)
        if not seq_date:
            seq_date = self._create_date_range_seq(dt)
        return seq_date.with_context(ir_sequence_date_range=seq_date.date_from)._next()

    @api.multi
    def next_by_id(self):
        """ Draw an interpolated string using the specified sequence."""
        self.check_access_rights('read')
        return self._next()

    @api.model
    def next_by_code(self, sequence_code):
        """ Draw an interpolated string using a sequence with the requested code.
            If several sequences with the correct code are available to the user
            (multi-company cases), the one from the user's current company will
            be used.

            :param dict context: context dictionary may contain a
                ``force_company`` key with the ID of the company to
                use instead of the user's current company for the
                sequence selection. A matching sequence for that
                specific company will get higher priority.
        """
        self.check_access_rights('read')
        company_ids = self.env['res.company'].search([]).ids + [False]
        seq_ids = self.search(['&', ('code', '=', sequence_code), ('company_id', 'in', company_ids)])
        if not seq_ids:
            _logger.debug("No ir.sequence has been found for code '%s'. Please make sure a sequence is set for current company." % sequence_code)
            return False
        force_company = self._context.get('force_company')
        if not force_company:
            force_company = self.env.user.company_id.id
        preferred_sequences = [s for s in seq_ids if s.company_id and s.company_id.id == force_company]
        seq_id = preferred_sequences[0] if preferred_sequences else seq_ids[0]
        return seq_id._next()

    @api.model
    def get_id(self, sequence_code_or_id, code_or_id='id'):
        """ Draw an interpolated string using the specified sequence.

        The sequence to use is specified by the ``sequence_code_or_id``
        argument, which can be a code or an id (as controlled by the
        ``code_or_id`` argument. This method is deprecated.
        """
        _logger.warning("ir_sequence.get() and ir_sequence.get_id() are deprecated. "
                        "Please use ir_sequence.next_by_code() or ir_sequence.next_by_id().")
        if code_or_id == 'id':
            return self.browse(sequence_code_or_id).next_by_id()
        else:
            return self.next_by_code(sequence_code_or_id)

    @api.model
    def get(self, code):
        """ Draw an interpolated string using the specified sequence.

        The sequence to use is specified by its code. This method is
        deprecated.
        """
        return self.get_id(code, 'code')


class IrSequenceDateRange(models.Model):
    _name = 'ir.sequence.date_range'
    _rec_name = "sequence_id"

    def _get_number_next_actual(self):
        '''Return number from ir_sequence row when no_gap implementation,
        and number from postgres sequence when standard implementation.'''
        for seq in self:
            if seq.sequence_id.implementation != 'standard':
                seq.number_next_actual = seq.number_next
            else:
                # get number from postgres sequence. Cannot use currval, because that might give an error when
                # not having used nextval before.
                self._cr.execute("SELECT last_value, increment_by, is_called FROM ir_sequence_%03d_%03d" % (seq.sequence_id.id, seq.id))
                (last_value, increment_by, is_called) = self._cr.fetchone()
                if is_called:
                    seq.number_next_actual = last_value + increment_by
                else:
                    seq.number_next_actual = last_value

    def _set_number_next_actual(self):
        for seq in self:
            seq.write({'number_next': seq.number_next_actual or 0})

    @api.model
    def default_get(self, fields):
        result = super(IrSequenceDateRange, self).default_get(fields)
        result['number_next_actual'] = 1
        return result

    date_from = fields.Date(string='From', required=True)
    date_to = fields.Date(string='To', required=True)
    sequence_id = fields.Many2one("ir.sequence", string='Main Sequence', required=True, ondelete='cascade')
    number_next = fields.Integer(string='Next Number', required=True, default=1, help="Next number of this sequence")
    number_next_actual = fields.Integer(compute='_get_number_next_actual', inverse='_set_number_next_actual',
                                        string='Next Number',
                                        help="Next number that will be used. This number can be incremented "
                                             "frequently so the displayed value might already be obsolete")

    def _next(self):
        if self.sequence_id.implementation == 'standard':
            number_next = _select_nextval(self._cr, 'ir_sequence_%03d_%03d' % (self.sequence_id.id, self.id))
        else:
            number_next = _update_nogap(self, self.sequence_id.number_increment)
        return self.sequence_id.get_next_char(number_next)

    @api.multi
    def _alter_sequence(self, number_increment=None, number_next=None):
        for seq in self:
            _alter_sequence(self._cr, "ir_sequence_%03d_%03d" % (seq.sequence_id.id, seq.id), number_increment=number_increment, number_next=number_next)

    @api.model
    def create(self, values):
        """ Create a sequence, in implementation == standard a fast gaps-allowed PostgreSQL sequence is used.
        """
        seq = super(IrSequenceDateRange, self).create(values)
        main_seq = seq.sequence_id
        if main_seq.implementation == 'standard':
            _create_sequence(self._cr, "ir_sequence_%03d_%03d" % (main_seq.id, seq.id), main_seq.number_increment, values.get('number_next_actual', 1))
        return seq

    @api.multi
    def unlink(self):
        _drop_sequences(self._cr, ["ir_sequence_%03d_%03d" % (x.sequence_id.id, x.id) for x in self])
        return super(IrSequenceDateRange, self).unlink()

    @api.multi
    def write(self, values):
        if values.get('number_next'):
            seq_to_alter = self.filtered(lambda seq: seq.sequence_id.implementation == 'standard')
            seq_to_alter._alter_sequence(number_next=values.get('number_next'))
        return super(IrSequenceDateRange, self).write(values)
