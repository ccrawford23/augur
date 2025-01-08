"""Add commit message table

Revision ID: 29
Revises: 28
Create Date: 2024-07-25 12:02:57.185867

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '29'
down_revision = '28'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('commit_messages',
    sa.Column('cmt_msg_id', sa.BigInteger(), server_default=sa.text("nextval('augur_data.commits_cmt_id_seq'::regclass)"), nullable=False),
    sa.Column('repo_id', sa.BigInteger(), nullable=False),
    sa.Column('cmt_msg', sa.String(), nullable=False),
    sa.Column('cmt_hash', sa.String(length=80), nullable=False),
    sa.Column('tool_source', sa.String(), nullable=True),
    sa.Column('tool_version', sa.String(), nullable=True),
    sa.Column('data_source', sa.String(), nullable=True),
    sa.Column('data_collection_date', postgresql.TIMESTAMP(precision=0), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=True),
    sa.ForeignKeyConstraint(['repo_id'], ['augur_data.repo.repo_id'], onupdate='CASCADE', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('cmt_msg_id'),
    sa.UniqueConstraint('repo_id', 'cmt_hash', name='commit-message-insert-unique'),
    schema='augur_data',
    comment='This table holds commit messages'
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('commit_messages', schema='augur_data')
    # ### end Alembic commands ###